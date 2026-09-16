"""pywebview 的 js_api 门面：校验、转发、JSON 化。"""

from __future__ import annotations

import collections
import re
import threading
import time
from typing import Any

import requests
import urllib3

from . import __version__
from .api import DuifeneApi
from .config import IniStore
from .errors import AuthError
from .location import LocationResolver
from .login_watch import LoginSentinel
from .models import Course
from .processor import ActivityProcessor
from .push import PushMonitor
from .rate_limit import RateLimiter
from .settings import DERIVED_FIELDS, SETTING_LIMITS, Settings
from .watcher import Watcher

# 日志级别推断关键字（顺序即优先级）。
_ERROR_MARKERS = ("失败", "错误", "异常", "未能")
_WARN_MARKERS = ("限流", "频繁", "静默")
_SUCCESS_MARKERS = ("成功", "监听其他用户签到")


class Bridge:
    """前端可调用的门面。

    所有方法返回 JSON 可序列化的 ``dict``，且**绝不向 JS 抛异常**——失败统一以
    ``{"ok": False, "message": ...}`` 返回。

    日志经线程安全的 ``deque`` 中转，前端通过 :meth:`drain_logs` 轮询拉取，这是
    唯一允许的 UI 更新通道（watcher 线程绝不触碰 webview）。
    """

    def __init__(self, settings: Settings) -> None:
        """组装依赖；``settings`` 会被就地加载已持久化的配置。"""
        self._settings = settings
        self._store = IniStore(settings.ini_filename)
        self._load_persisted_settings()
        self._limiter = RateLimiter(settings, self._log)
        self._session = self._build_session(settings)
        self._window: Any = None  # 由 main.py 注入，用于窗口控制（无边框模式）
        self._api = DuifeneApi(self._session, settings, self._limiter)
        self._push = PushMonitor(
            settings, self._api, lambda: self._api.cached_user_id, self._log
        )
        self._resolver = LocationResolver(settings, self._api)
        self._processor = ActivityProcessor(
            settings, self._api, self._push, self._resolver, self._store, self._log
        )
        self._watcher = Watcher(
            settings,
            self._api,
            self._processor,
            self._push,
            self._log,
        )
        self._courses: list[dict[str, Any]] = []
        self._course: Course | None = None
        # 登录态的权威标志：成功拉到课程时置真，哨兵探测确认失效时置否。
        # 不能只看 Cookie 是否为空——网关下发的 tgw_l7_route 路由 Cookie
        # 在未认证时也存在，会造成误判。
        self._logged_in = False
        # 会话读写的互斥锁：登录动作（会先清空会话）与登录态探测必须串行，
        # 否则登录中途的空会话会被探测误判为「已失效」并清掉刚拿到的 Cookie。
        # 用可重入锁：登录方法整体持锁的同时，内部快照/恢复辅助函数也各自
        # 加锁——它们从失败分支（锁外）调用时同样需要保护。
        self._auth_lock = threading.RLock()
        self._sentinel = LoginSentinel(
            settings,
            self._api,
            self._on_login_invalid,
            self._log,
            lambda: self._logged_in,
            self._auth_lock,
        )
        self._logs: collections.deque[dict[str, Any]] = collections.deque(maxlen=2000)
        self._log_lock = threading.Lock()
        self._log_seq = 0
        self._pending_notice: dict[str, str] | None = None

    # ─ 内部：会话与日志 ─────────────────────────────────────────────
    def _build_session(self, settings: Settings) -> requests.Session:
        """构造全局复用的 HTTP 会话。"""
        urllib3.disable_warnings()
        session = requests.Session()
        session.headers["User-Agent"] = settings.user_agent
        session.trust_env = False
        self._apply_session_config(session)
        return session

    def _apply_session_config(self, session: requests.Session) -> None:
        """把代理与 TLS 开关同步到会话（配置变更后需重新应用）。"""
        session.verify = self._settings.verify_tls
        session.proxies = self._settings.proxies or {}

    def _load_persisted_settings(self) -> None:
        """从 ini 的 `[SETTINGS]` 段加载已保存的运行参数。

        ini 中同时存有派生字段（如 ``verify_tls``），而 :meth:`Settings.apply`
        拒绝直接设置派生字段、且会因此整体不写入；故这里先滤除派生字段，只
        提交可编辑字段，否则重启后全部设置都无法恢复。
        """
        saved = self._store.load_settings()
        if not saved:
            return
        editable = {
            key: value for key, value in saved.items() if key not in DERIVED_FIELDS
        }
        self._settings.apply(editable)

    @staticmethod
    def _level_of(text: str) -> str:
        """由文本关键字推断日志级别，返回 success / error / warning / info。"""
        if any(marker in text for marker in _ERROR_MARKERS):
            return "error"
        if any(marker in text for marker in _WARN_MARKERS):
            return "warning"
        if any(marker in text for marker in _SUCCESS_MARKERS):
            return "success"
        return "info"

    def _log(self, text: str) -> None:
        """线程安全的日志入队（实现 ``LogFn``）；文本含换行时按行拆分。"""
        now = time.time()
        with self._log_lock:
            for line in str(text).splitlines() or [""]:
                line = line.rstrip()
                if not line:
                    continue
                self._log_seq += 1
                self._logs.append(
                    {
                        "id": self._log_seq,
                        "text": line,
                        "ts": now,
                        "level": self._level_of(line),
                    }
                )

    def _notify(self, level: str, text: str) -> None:
        """记录异步通知：写日志队列并置待读横幅。"""
        self._log(text)
        # 登录失效通知（含「重新登录」）由常驻哨兵触发，它已直接清会话、
        # 翻状态位；这里再兜一次，保证任何来源的此类文案都同步登录态。
        if "重新登录" in text:
            self._logged_in = False
        self._pending_notice = {"level": level, "text": text}

    def _on_login_invalid(self) -> None:
        """登录态经探测确认为失效时的处置：停监控、翻转状态位、告警。

        由常驻哨兵线程调用（已在锁外）。清除课程并置登录标志为假，使前端
        状态位与课程选择器同步复位；停监控避免后续轮询继续用失效会话发请求。
        """
        self._logged_in = False
        self._course = None
        self._watcher.set_course(None)
        self._watcher.stop()
        self._notify("error", "登录状态失效，请重新登录账号")

    # ─ 内部：课程 / 登录共用逻辑 ────────────────────────────────────
    def _refresh_courses(self) -> str:
        """拉取课程列表并选中首门；返回错误文案，成功返回空串。

        Raises:
            AuthError: 接口返回登录失效。
        """
        with self._auth_lock:
            self._courses = self._api.fetch_courses()
            if not self._courses:
                return "未获取到课程"
            self._logged_in = True
            self._select_course(self._courses[0]["CourseID"])
            return ""

    def _select_course(self, course_id: str) -> None:
        """按 ``CourseID`` 设为当前课程。"""
        for raw in self._courses:
            if str(raw["CourseID"]) == str(course_id):
                self._course = Course(
                    course_id=str(raw["CourseID"]),
                    class_id=str(raw["TClassID"]),
                    name=str(raw["CourseName"]),
                )
                self._watcher.set_course(self._course)
                return

    def _courses_payload(self) -> list[dict[str, str]]:
        """课程列表的前端表示。"""
        return [
            {
                "id": str(raw["CourseID"]),
                "class_id": str(raw["TClassID"]),
                "name": str(raw["CourseName"]),
            }
            for raw in self._courses
        ]

    @staticmethod
    def _parse_cookie(cookie: str) -> dict[str, str]:
        """解析 `k=v; k=v` 形式的 Cookie 字符串。"""
        parsed: dict[str, str] = {}
        for pair in cookie.split("; "):
            key, _, value = pair.partition("=")
            if key:
                parsed[key] = value
        return parsed

    @staticmethod
    def _cookie_string(cookies: dict[str, str]) -> str:
        """把 Cookie 映射序列化为 `k=v; k=v` 字符串。"""
        return "; ".join(f"{key}={value}" for key, value in cookies.items())

    def _snapshot_login(self) -> dict[str, Any]:
        """快照当前登录态（会话 Cookie、课程缓存与登录标志），供登录失败时回滚。"""
        with self._auth_lock:
            return {
                "cookies": self._session.cookies.get_dict(),
                "logged_in": self._logged_in,
                "courses": list(self._courses),
                "course": self._course,
            }

    def _restore_login(self, snapshot: dict[str, Any]) -> None:
        """把内存登录态恢复到快照（登录失败路径专用）。

        ini 中的 Cookie 只在登录**验证成功**后才写入、失败路径从不写盘，故这里
        只回滚内存态即可保证旧 Cookie 完整保留；快照本身无 Cookie 时相当于清空。
        """
        with self._auth_lock:
            self._session.cookies.clear()
            cookies = snapshot.get("cookies") or {}
            if cookies:
                self._session.cookies.update(cookies)
            self._logged_in = bool(snapshot.get("logged_in"))
            self._courses = list(snapshot.get("courses") or [])
            self._course = snapshot.get("course")
            self._watcher.set_course(self._course)

    # ── js_api ───────────────────────────────────────────────────────
    def bootstrap(self) -> dict[str, Any]:
        """初始化：创建配置、恢复登录态、返回课程与历史日志。"""
        message = ""
        try:
            if self._store.ensure_exists():
                self._session.get(self._settings.host, timeout=10)
            else:
                cookie = self._store.load_cookie()
                if cookie:
                    self._session.cookies.update(self._parse_cookie(cookie))
                    message = self._refresh_courses()
        except AuthError as exc:
            self._api.clear_session()
            self._logged_in = False
            message = f"{exc} 请重新登录。"
        except (requests.ConnectionError, requests.Timeout):
            message = "未检测到互联网连接，请检查你的网络设置。"
        except Exception as exc:  # noqa: BLE001 - 门面绝不抛出
            message = f"初始化失败：{exc!s}"
        # 哨兵常驻：只要已登录就持续探测 Cookie 有效性，与是否在监听无关。
        self._sentinel.start()
        return {
            "ok": True,
            "version": __version__,
            "courses": self._courses_payload(),
            "selected": self._course.course_id if self._course else "",
            "logged_in": self._logged_in,
            "logs": self.drain_logs(0).get("items", []),
            "message": message,
        }

    def login_by_link(self, link: str) -> dict[str, Any]:
        """微信回调链接登录。

        登录失败时回滚登录前快照，且**从不写盘**——避免一次失败的尝试抹掉 ini
        中既有的有效 Cookie；Cookie 只在验证成功后才持久化。
        """
        try:
            snapshot = self._snapshot_login()
            match = re.search(r"(?<=code=)\S{32}", str(link or ""))
            if match is None:
                return {"ok": False, "message": "链接有误"}
            # 整段登录流程持锁：中间会 clear_session 造成空会话中间态，
            # 必须与哨兵探测互斥，否则哨兵会在此刻误判为「已失效」。
            with self._auth_lock:
                # 登录请求会先清空会话；新 Cookie 由 Set-Cookie 落到 session，
                # 只能从 session 读取（response.request.headers 里恒为 None）。
                self._api.login_by_link(match.group(0))
                cookies = self._session.cookies.get_dict()
                if not cookies:
                    self._restore_login(snapshot)
                    return {"ok": False, "message": "登录失败，未取得登录凭证"}
                error = self._refresh_courses()
                if error:
                    self._restore_login(snapshot)
                    return {"ok": False, "message": error}
                self._store.save_cookie(self._cookie_string(cookies))
            self._sentinel.notify_logged_in()
            # 登录成功即清空此前所有运行日志：新会话的日志不应与旧会话混在一起。
            self._clear_logs()
            with self._log_lock:
                next_id = self._log_seq
            return {"ok": True, "message": "登录成功", "next_id": next_id}
        except AuthError as exc:
            self._restore_login(snapshot)
            return {"ok": False, "message": f"{exc} 请重新登录。"}
        except Exception as exc:  # noqa: BLE001
            self._restore_login(snapshot)
            return {"ok": False, "message": f"登录失败：{exc!s}"}

    def login_by_password(self, username: str, password: str) -> dict[str, Any]:
        """账号密码登录。

        登录失败时回滚登录前快照，且**从不写盘**——避免一次失败的尝试抹掉 ini
        中既有的有效 Cookie；Cookie 只在验证成功后才持久化。
        """
        try:
            snapshot = self._snapshot_login()
            # 同 login_by_link：整段持锁，避免登录中途的空会话被哨兵误判。
            with self._auth_lock:
                message = self._api.login_by_password(
                    str(username or ""), str(password or "")
                )
                if not message:
                    self._restore_login(snapshot)
                    return {"ok": False, "message": "登录失败"}
                if message != "登录成功":
                    # 服务端明确拒绝（如密码错误）：恢复原登录态。
                    self._restore_login(snapshot)
                    return {"ok": False, "message": message}
                cookies = self._session.cookies.get_dict()
                error = self._refresh_courses()
                if error:
                    self._restore_login(snapshot)
                    return {"ok": False, "message": error}
                self._store.save_cookie(self._cookie_string(cookies))
            self._sentinel.notify_logged_in()
            # 同链接登录：成功即清空旧日志，返回新序号供前端复位游标。
            self._clear_logs()
            with self._log_lock:
                next_id = self._log_seq
            return {"ok": True, "message": "登录成功", "next_id": next_id}
        except AuthError as exc:
            self._restore_login(snapshot)
            return {"ok": False, "message": f"{exc} 请重新登录。"}
        except Exception as exc:  # noqa: BLE001
            self._restore_login(snapshot)
            return {"ok": False, "message": f"登录失败：{exc!s}"}

    def select_course(self, course_id: str) -> dict[str, Any]:
        """切换当前课程。"""
        try:
            self._select_course(str(course_id or ""))
            if self._course is None:
                return {"ok": False, "message": "课程不存在"}
            return {"ok": True, "message": f"已选择【{self._course.name}】"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"切换课程失败：{exc!s}"}

    def get_settings(self) -> dict[str, Any]:
        """返回用户可编辑的运行参数（派生字段不下发，故前端不渲染）。

        ``limits`` 为各字段的 ``[下限, 上限]``，无限制的字段不出现。
        """
        try:
            limits = {name: list(bounds) for name, bounds in SETTING_LIMITS.items()}
            return {
                "ok": True,
                "values": self._settings.editable_values(),
                "limits": limits,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"读取配置失败：{exc!s}"}

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """校验、应用并持久化一批配置更新。

        校验不通过时不修改任何字段（原子性）；应用成功后立刻把代理与 TLS 开关
        同步到现有会话，并落盘。
        """
        try:
            if not isinstance(patch, dict):
                return {"ok": False, "message": "参数格式错误"}
            error = self._settings.apply(patch)
            if error:
                return {"ok": False, "message": error}
            self._apply_session_config(self._session)
            self._store.save_settings(self._settings.persistable_values())
            return {
                "ok": True,
                "message": "设置已保存",
                "values": self._settings.editable_values(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"保存设置失败：{exc!s}"}

    def start_watching(self) -> dict[str, Any]:
        """启动监控。"""
        try:
            if self._course is None or self._course.course_id == "0":
                return {"ok": False, "message": "课程未正确加载，请重新登录"}
            self._watcher.start()
            return {"ok": True, "message": f"正在监听【{self._course.name}】"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"启动监听失败：{exc!s}"}

    def stop_watching(self) -> dict[str, Any]:
        """停止监控。"""
        try:
            self._watcher.stop()
            return {"ok": True, "message": "已停止监听"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"停止失败：{exc!s}"}

    # ─ 窗口控制（无边框模式下由前端自绘按钮调用）──────────────────
    def set_window(self, window: Any) -> None:
        """注入 pywebview 窗口对象，供前端控制窗口（main.py 创建窗口后调用）。"""
        self._window = window

    def window_minimize(self) -> dict[str, Any]:
        """最小化窗口。"""
        try:
            if self._window is not None:
                self._window.minimize()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"最小化失败：{exc!s}"}

    def window_close(self) -> dict[str, Any]:
        """关闭窗口（先停监控与哨兵，确保线程与 WS 被清理）。"""
        try:
            self._watcher.stop()
            self._sentinel.stop()
            if self._window is not None:
                self._window.destroy()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"关闭失败：{exc!s}"}

    def get_state(self) -> dict[str, Any]:
        """返回当前运行状态，供前端刷新指标。"""
        try:
            notice = self._pending_notice
            self._pending_notice = None
            return {
                "running": self._watcher.is_running,
                "course_name": self._course.name if self._course else "",
                "limiter_remaining_s": round(self._limiter.remaining(), 1),
                "last_tick_ts": self._watcher.last_tick_ts,
                "logged_in": self._logged_in,
                "notice": notice,
            }
        except Exception:  # noqa: BLE001
            return {
                "running": False,
                "course_name": "",
                "limiter_remaining_s": 0.0,
                "last_tick_ts": 0.0,
                "logged_in": False,
                "notice": None,
            }

    def clear_logs(self) -> dict[str, Any]:
        """清空日志队列，供前端复位显示时同步。

        ``next_id`` 为清空后的日志序号，前端据此复位 ``logCursor``（序号单调
        递增，故该值可直接用作新游标）。
        """
        try:
            self._clear_logs()
            with self._log_lock:
                next_id = self._log_seq
            return {"ok": True, "next_id": next_id}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"清空日志失败：{exc!s}"}

    def _clear_logs(self) -> None:
        """清空日志队列。

        只清队列、不重置 ``_log_seq``：id 保持单调递增，前端的
        ``logCursor`` 便永不失效——若把序号归零，前端已持有的旧游标会让
        新日志的 id 小于游标而被永久漏掉。
        """
        with self._log_lock:
            self._logs.clear()

    def drain_logs(self, since_id: int = 0) -> dict[str, Any]:
        """取出 id 大于 ``since_id`` 的日志；每项含 ``id/text/ts/level``。"""
        try:
            since = int(since_id or 0)
        except (TypeError, ValueError):
            since = 0
        with self._log_lock:
            items = [item for item in self._logs if item["id"] > since]
            next_id = self._log_seq
        return {"next_id": next_id, "items": items}
