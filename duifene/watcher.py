"""GUI 无关的 App 签到监控线程。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime

from .api import DuifeneApi
from .errors import AuthError, LogFn
from .models import Course
from .processor import ActivityProcessor
from .settings import Settings


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
        self._last_tick_ts = 0.0
        self._in_window: bool | None = None
        self._lock = threading.Lock()

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
        self._in_window = None
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
        """轮询主体；认证失效时停止，普通异常按退避重试。"""
        while not self._stop.is_set():
            try:
                self._tick()
            except AuthError as exc:
                with self._lock:
                    course = self._course
                self._log(self._course_prefix(course, f"{exc!s}，监听已停止"))
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
            self._sleep(
                random.uniform(
                    self._settings.poll_min_s,
                    self._settings.poll_max_s,
                )
            )

    def _tick(self) -> None:
        """读取 App 当前签到并交给处理器。"""
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
            return
        activity = self._api.fetch_active_activity(course.course_id)
        if activity is None:
            self._consecutive_errors = 0
            return
        self._processor.process(activity, course)
        self._consecutive_errors = 0
