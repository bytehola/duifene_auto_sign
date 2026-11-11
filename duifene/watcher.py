"""GUI 无关的 App 签到监控线程。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime

from .api import DuifeneApi
from .app_api import AppCheckIn, CHINA_TZ, parse_checkin_time
from .errors import AppApiError, AuthError, LogFn, PermissionDenied, WebSessionError
from .models import Course, Outcome
from .processor import ActivityProcessor
from .settings import Settings

NUMERIC_CODE_POLL_S = 0.2
NUMERIC_ERROR_BACKOFF_MAX_S = 2.0
NUMERIC_PRIORITY_WINDOW_S = 5.0


class Watcher:
    """在后台线程中轮询当前课程的 App 签到。"""

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        processor: ActivityProcessor,
        log: LogFn,
    ) -> None:
        self._settings = settings
        self._api = api
        self._processor = processor
        self._log = log
        self._course: Course | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_errors = 0
        self._numeric_failures = 0
        self._last_tick_ts = 0.0
        self._in_window: bool | None = None
        self._lock = threading.Lock()
        self._seen_activities: set[str] = set()
        self._numeric_deadline: datetime | None = None
        self._pending_numeric: dict[str, AppCheckIn] = {}

    @property
    def is_running(self) -> bool:
        """监控线程是否正在运行。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_tick_ts(self) -> float:
        """最近一轮轮询的时间戳。"""
        return self._last_tick_ts

    def set_course(self, course: Course | None) -> None:
        """设置当前课程；``None`` 表示清空。"""
        with self._lock:
            self._course = course

    def start(self) -> None:
        """启动监控线程。"""
        if not self._api.app_authenticated:
            raise AuthError("登录状态已失效，请重新登录账号")
        self.stop()
        self._stop.clear()
        self._processor.reset()
        self._consecutive_errors = 0
        self._numeric_failures = 0
        self._in_window = None
        self._seen_activities.clear()
        self._numeric_deadline = None
        self._pending_numeric.clear()
        with self._lock:
            course = self._course
        self._log(self._course_prefix(course, "开始监听"))
        self._thread = threading.Thread(
            target=self._run,
            name="duifene-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """停止监控线程。"""
        was_running = self.is_running
        with self._lock:
            course = self._course
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        if was_running:
            self._log(self._course_prefix(course, "已停止监听"))

    @staticmethod
    def _course_prefix(course: Course | None, message: str) -> str:
        """给日志加课程前缀。"""
        return f"课程: {course.name} {message}" if course else message

    def _sleep(self, seconds: float) -> None:
        """可中断休眠。"""
        self._stop.wait(max(0.0, seconds))

    def _in_monitor_window(self) -> bool:
        """当前是否处于监听时间窗。"""
        hour = datetime.now().hour
        return (
            self._settings.monitor_start_hour
            <= hour
            < self._settings.monitor_end_hour
        )

    def _run(self) -> None:
        """认证失效或权限拒绝时停止，普通异常按退避重试。"""
        next_discovery = 0.0
        next_material = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                # 截止前优先完成短取码请求，避免较慢的活动发现恰好跨过码开放窗口。
                if now >= next_discovery and not self._numeric_is_urgent():
                    self._tick()
                    next_discovery = time.monotonic() + self._poll_delay()
                if self._pending_numeric and time.monotonic() >= next_material:
                    self._numeric_tick()
                    next_material = time.monotonic() + self._numeric_poll_delay()
                self._consecutive_errors = 0
            except AuthError as exc:
                with self._lock:
                    course = self._course
                self._log(self._course_prefix(course, f"{exc!s}，监听已停止"))
                self._stop.set()
                break
            except (PermissionDenied, WebSessionError) as exc:
                with self._lock:
                    course = self._course
                self._log(
                    self._course_prefix(course, f"监听失败：{exc!s}，监听已停止")
                )
                self._stop.set()
                break
            except Exception as exc:  # noqa: BLE001 - 单轮失败不终止监控
                self._consecutive_errors += 1
                backoff = min(
                    self._settings.poll_min_s * (2**self._consecutive_errors),
                    self._settings.error_backoff_max_s,
                )
                with self._lock:
                    course = self._course
                self._log(
                    self._course_prefix(
                        course,
                        f"监控异常：{exc!s}（{int(backoff)} 秒后重试）",
                    )
                )
                self._sleep(backoff)
                continue
            if self._stop.is_set():
                break
            now = time.monotonic()
            waits = []
            if self._pending_numeric:
                waits.append(max(0.0, next_material - now))
            if not self._numeric_is_urgent():
                waits.append(max(0.0, next_discovery - now))
            self._sleep(min(waits) if waits else self._poll_delay())

    def _poll_delay(self) -> float:
        """活动发现始终沿用界面配置；取码使用自己的短周期。"""
        return random.uniform(self._settings.poll_min_s, self._settings.poll_max_s)

    def _numeric_poll_delay(self) -> float:
        """正常取码等待 0.2 秒；仅取码失败按 0.4、0.8、1.6、2 秒退避。"""
        # 四次失败已经封顶，避免长期失败时指数计算溢出。
        return min(
            NUMERIC_CODE_POLL_S * (2 ** min(self._numeric_failures, 4)),
            NUMERIC_ERROR_BACKOFF_MAX_S,
        )

    def _numeric_is_urgent(self) -> bool:
        if not self._pending_numeric or self._numeric_deadline is None:
            return False
        remaining = (self._numeric_deadline - datetime.now(CHINA_TZ)).total_seconds()
        return 0 < remaining <= NUMERIC_PRIORITY_WINDOW_S

    def _set_pending_numeric(self, activities: list[AppCheckIn]) -> None:
        if not activities or not self._pending_numeric.keys() & {a.activity_id for a in activities}:
            self._numeric_failures = 0
        self._pending_numeric = {a.activity_id: a for a in activities}
        self._numeric_deadline = min(
            (parse_checkin_time(a.valid_date) for a in activities), default=None,
        )

    def _numeric_tick(self) -> None:
        """快速取码只读网页材料；不重跑学生记录与 App 详情发现。"""
        self._last_tick_ts = time.time()
        if self._stop.is_set():
            return
        with self._lock:
            course = self._course
        if course is None or not self._in_monitor_window():
            self._set_pending_numeric([])
            return
        if self._api.limiter.is_blocking():
            return
        pending = [a for a in self._pending_numeric.values() if not a.has_ended()]
        if not pending:
            self._set_pending_numeric([])
            return
        try:
            refreshed = self._api.refresh_numeric_materials(course, pending)
            keep = []
            for activity in refreshed:
                if self._stop.is_set():
                    break
                outcome = self._processor.process(activity, course)
                if outcome is Outcome.DEFERRED:
                    keep.append(activity)
        except (AuthError, PermissionDenied, WebSessionError):
            raise  # 身份/权限问题仍交给 _run 停止监听。
        except AppApiError as exc:
            previous_delay = self._numeric_poll_delay()
            self._numeric_failures += 1
            delay = self._numeric_poll_delay()
            if self._numeric_failures == 1 or previous_delay < NUMERIC_ERROR_BACKOFF_MAX_S <= delay:
                self._log(self._course_prefix(
                    course, f"数字取码异常：{exc!s}（{delay:g} 秒后重试）",
                ))
            return  # 不进入活动发现的长退避，也不重置发现的错误计数。
        self._numeric_failures = 0
        self._set_pending_numeric(keep)

    def _tick(self) -> None:
        """从当前学生的记录发现活动，交给处理器。"""
        self._last_tick_ts = time.time()
        if self._stop.is_set():
            return
        if not self._api.app_authenticated:
            raise AuthError("登录状态已失效，请重新登录账号")
        with self._lock:
            course = self._course
        in_window = self._in_monitor_window()
        if in_window != self._in_window:
            self._in_window = in_window
            window = (
                f"{self._settings.monitor_start_hour}–"
                f"{self._settings.monitor_end_hour} 时"
            )
            self._log(
                self._course_prefix(
                    course,
                    f"进入监听时间窗（{window}），开始轮询"
                    if in_window
                    else f"当前不在监听时间窗（{window}），暂不轮询",
                )
            )
        if not in_window or course is None:
            self._set_pending_numeric([])
            return
        discovered = self._api.fetch_active_activities(course, include_numeric_materials=False)
        if not discovered and self._pending_numeric:
            # 空列表可能是短暂缺失；仅保留当前学生/班级尚有效的候选，不能无限续命。
            identity = self._api.identity
            pending = [
                a for a in self._pending_numeric.values()
                if identity is not None and a.student_id == identity.user_id
                and a.class_id == course.class_id and a.has_valid_times
                and not a.has_ended() and not a.is_deleted and not a.is_attended
                and a.age_seconds() is not None
                and 0 <= a.age_seconds() <= self._settings.stale_sign_minutes * 60
            ]
            self._set_pending_numeric(pending)
            self._consecutive_errors = 0
            return
        pending = []
        for activity in discovered:
            if self._stop.is_set():
                break
            if activity.activity_id not in self._seen_activities:
                self._seen_activities.add(activity.activity_id)
                self._log(self._course_prefix(course, f"发现进行中的签到 {activity.activity_id}"))
            outcome = self._processor.process(activity, course)
            missing_numeric_code = not (
                len(activity.code) == 4 and activity.code.isascii() and activity.code.isdigit()
            )
            if activity.check_type == "1" and missing_numeric_code and activity.has_valid_times:
                if outcome is Outcome.DEFERRED:
                    pending.append(activity)
        self._set_pending_numeric(pending)
        self._consecutive_errors = 0
