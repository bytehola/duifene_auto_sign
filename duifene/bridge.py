"""pywebview 的 js_api 门面：校验、转发、JSON 化。"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

import requests
import urllib3

from . import __version__
from .api import DuifeneApi
from .config import IniStore
from .credentials import CredentialStore
from .errors import AppApiError, AuthError
from .models import Course
from .processor import ActivityProcessor
from .rate_limit import RateLimiter
from .session_watch import SessionKeeper
from .settings import PERSISTABLE_FIELDS, SETTING_LIMITS, Settings
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
        self._credentials = CredentialStore(self._store)
        self._load_persisted_settings()
        self._limiter = RateLimiter(settings, self._log)
        self._session = self._build_session(settings)
        self._window: Any = None  # 由 main.py 注入，用于窗口控制（无边框模式）
        self._api = DuifeneApi(
            self._session,
            settings,
            self._limiter,
            self._store,
        )
        self._processor = ActivityProcessor(
            settings,
            self._api,
            self._log,
        )
        self._watcher = Watcher(
            settings,
            self._api,
            self._processor,
            self._log,
        )
        self._session_keeper = SessionKeeper(self._api, self._log)
        self._courses: list[Course] = []
        self._course: Course | None = None
        self._user_account = ""
        self._user_id = ""
        # 登录态的权威标志：App 登录并拉到课程时置真，Token 失效时置否。
        self._logged_in = False
        # 登录与状态清理串行执行，避免登录响应写入身份时被另一处清空。
        self._auth_lock = threading.RLock()
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
        editable_fields = set(PERSISTABLE_FIELDS)
        editable = {
            key: value for key, value in saved.items() if key in editable_fields
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
        if "重新登录" in text:
            self._logged_in = False
        self._pending_notice = {"level": level, "text": text}

    def _on_login_invalid(self) -> None:
        """App 登录态失效时清理课程、停止监听并告警。"""
        self._clear_login_state()
        self._notify("error", "登录状态失效，请重新登录账号")

    def _clear_login_state(self) -> None:
        """清空 App 登录态与当前课程。"""
        self._session_keeper.stop()
        self._api.clear_session()
        self._logged_in = False
        self._courses = []
        self._course = None
        self._user_account = ""
        self._user_id = ""
        self._watcher.set_course(None)
        self._watcher.stop()

    # ─ 内部：课程 / 登录共用逻辑 ────────────────────────────────────
    def _refresh_courses(self) -> str:
        """拉取课程列表并选中首门；返回错误文案，成功返回空串。

        Raises:
            AuthError: 接口返回登录失效。
        """
        with self._auth_lock:
            self._courses = self._api.fetch_courses()
            if not self._courses:
                return "未返回课程"
            self._logged_in = True
            self._select_course(self._course_key(self._courses[0]))
            self._refresh_user_identity()
            return ""

    def _refresh_user_identity(self) -> None:
        """缓存 App 登录账号与用户 ID。"""
        profile = self._api.fetch_user_profile()
        if profile is not None:
            self._user_account, self._user_id = profile

    def _select_course(self, course_id: str) -> None:
        """按课程 ID 设为当前课程。"""
        for course in self._courses:
            if self._course_key(course) == str(course_id):
                self._course = course
                self._watcher.set_course(self._course)
                return
        for course in self._courses:
            if course.course_id == str(course_id):
                self._course = course
                self._watcher.set_course(self._course)
                return

    @staticmethod
    def _course_key(course: Course) -> str:
        """生成课程与教学班的唯一前端标识。"""
        return f"{course.course_id}:{course.class_id}"

    def _courses_payload(self) -> list[dict[str, str]]:
        """课程列表的前端表示。"""
        return [
            {
                "id": self._course_key(course),
                "class_id": course.class_id,
                "name": course.name,
            }
            for course in self._courses
        ]

    # ── js_api ───────────────────────────────────────────────────────
    def bootstrap(self) -> dict[str, Any]:
        """初始化：创建配置并尝试恢复上次的 App 登录会话。

        登录只能通过账号密码取得 App Token；旧版本网页与扫码会话会被清除。
        """
        message = ""
        saved_credentials = self._credentials.load()
        try:
            self._store.ensure_exists()
            self._store.clear_legacy_login()
            if self._api.app_authenticated:
                message = self._refresh_courses()
            elif saved_credentials is not None:
                login_result = self.login_by_password(
                    saved_credentials.account,
                    saved_credentials.password,
                )
                if not login_result["ok"]:
                    message = str(login_result["message"])
        except AuthError as exc:
            self._clear_login_state()
            message = f"{exc} 请重新登录。"
        except AppApiError as exc:
            message = f"自动恢复登录失败：{exc!s}"
        except Exception as exc:  # noqa: BLE001 - 门面绝不抛出
            message = f"初始化失败：{exc!s}"
        if self._api.app_authenticated:
            self._session_keeper.start()
        return {
            "ok": True,
            "version": __version__,
            "courses": self._courses_payload(),
            "selected": self._course_key(self._course) if self._course else "",
            "logged_in": self._logged_in,
            "user_account": self._user_account,
            "user_id": self._user_id,
            "saved_account": (
                saved_credentials.account if saved_credentials is not None else ""
            ),
            "saved_password": (
                saved_credentials.password if saved_credentials is not None else ""
            ),
            "app_authenticated": self._api.app_authenticated,
            "logs": self.drain_logs(0).get("items", []),
            "message": message,
        }

    def login_by_password(self, username: str, password: str) -> dict[str, Any]:
        """账号密码登录。

        只使用 App 接口认证。本程序只保留这一种登录方式。
        """
        try:
            with self._auth_lock:
                self._user_account = str(username or "").strip()
                self._user_id = ""
                message = self._api.login_by_password(
                    str(username or ""), str(password or "")
                )
                if not message:
                    self._clear_login_state()
                    return {"ok": False, "message": "登录失败"}
                if message != "登录成功":
                    self._clear_login_state()
                    return {"ok": False, "message": message}
                error = self._refresh_courses()
                if error:
                    self._clear_login_state()
                    return {"ok": False, "message": error}
                self._credentials.save(username, password)
            self._session_keeper.start()
            self._clear_logs()
            with self._log_lock:
                next_id = self._log_seq
            return {
                "ok": True,
                "message": "登录成功",
                "next_id": next_id,
                "app_authenticated": self._api.app_authenticated,
            }
        except AuthError as exc:
            self._clear_login_state()
            return {"ok": False, "message": f"{exc} 请重新登录。"}
        except Exception as exc:  # noqa: BLE001
            self._clear_login_state()
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
            if not self._api.app_authenticated:
                self._on_login_invalid()
                return {
                    "ok": False,
                    "message": "登录状态失效，请重新登录账号",
                }
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
        """关闭窗口并停止监控线程。"""
        try:
            self._watcher.stop()
            self._session_keeper.stop()
            if self._window is not None:
                self._window.destroy()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"关闭失败：{exc!s}"}

    def get_state(self) -> dict[str, Any]:
        """返回当前运行状态，供前端刷新指标。"""
        try:
            if self._logged_in and not self._api.app_authenticated:
                self._on_login_invalid()
            notice = self._pending_notice
            self._pending_notice = None
            return {
                "running": self._watcher.is_running,
                "course_name": self._course.name if self._course else "",
                "limiter_remaining_s": round(self._limiter.remaining(), 1),
                "last_tick_ts": self._watcher.last_tick_ts,
                "logged_in": self._logged_in,
                "user_account": self._user_account,
                "user_id": self._user_id,
                "app_authenticated": self._api.app_authenticated,
                "notice": notice,
            }
        except Exception:  # noqa: BLE001
            return {
                "running": False,
                "course_name": "",
                "limiter_remaining_s": 0.0,
                "last_tick_ts": 0.0,
                "logged_in": False,
                "user_account": "",
                "user_id": "",
                "app_authenticated": False,
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
