"""App 登录会话后台保活线程。"""

from __future__ import annotations

import threading

from .api import DuifeneApi
from .errors import AppApiError, AuthError, LogFn

SESSION_CHECK_INTERVAL_S = 60.0


class SessionKeeper:
    """定期检查 App 访问 Token，并在临近过期时自动续期。"""

    def __init__(
        self,
        api: DuifeneApi,
        log: LogFn,
    ) -> None:
        self._api = api
        self._log = log
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error = ""

    @property
    def is_running(self) -> bool:
        """保活线程是否存活。"""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动会话保活线程；已在运行时直接返回。"""
        if self.is_running:
            return
        self._stop.clear()
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._run,
            name="duifene-session-keeper",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """停止会话保活线程。"""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        """周期性检查会话；临时网络错误不丢弃 Refresh Token。"""
        while not self._stop.is_set():
            self._wake.wait(SESSION_CHECK_INTERVAL_S)
            if self._stop.is_set():
                break
            self._wake.clear()
            try:
                if self._api.ensure_session_fresh():
                    self._log("登录状态已自动续期")
                self._last_error = ""
            except AuthError as exc:
                self._log(f"{exc!s}，后台续期已停止")
                self._stop.set()
                break
            except AppApiError as exc:
                message = str(exc)
                if message != self._last_error:
                    self._last_error = message
                    self._log(f"{message}，稍后重试")
