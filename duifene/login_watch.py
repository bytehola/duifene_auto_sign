"""登录态常驻哨兵：已登录期间周期性探测 Cookie 是否仍然有效。"""

from __future__ import annotations

import threading
from typing import Any, Callable

from .api import DuifeneApi
from .errors import LogFn
from .settings import Settings


class LoginSentinel:
    """只要处于已登录态，就按固定间隔探测 ``/AppCode/LoginInfo.ashx``。

    与 :class:`duifene.watcher.Watcher` 解耦：登录态探测不再依赖「开始监听」，
    只要账号已登录便持续运行。未登录时只等待、不发请求。

    探测结果三分：
        ``True``  已登录 —— 什么都不做；
        ``False`` 已失效 —— 清会话、回调 ``on_invalid``（由外壳停监控并告警）；
        ``None``  网络失败 —— 视为无法判定，**绝不**据此判定失效，避免临时
                  断网被误报为登录失效。

    探测与登录动作共享同一把互斥锁：登录请求会先清空会话，若此时被探测穿插，
    会读到「未登录」并被误判为失效。锁保证两者串行。
    """

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        on_invalid: Callable[[], None],
        log: LogFn,
        is_logged_in: Callable[[], bool],
        auth_lock: Any,
    ) -> None:
        """auth_lock 与登录动作共享，串行化会话读写。"""
        self._settings = settings
        self._api = api
        self._on_invalid = on_invalid
        self._log = log
        self._is_logged_in = is_logged_in
        self._auth_lock = auth_lock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """哨兵线程是否存活。"""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动哨兵线程（幂等：已在运行则直接返回）。"""
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="duifene-login-sentinel", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停止哨兵线程并等待其退出。"""
        self._stop.set()
        self._wake.set()  # 唤醒正在等待的线程，使其尽快看到停止标志
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    def notify_logged_in(self) -> None:
        """告知哨兵「刚刚登录成功」，立即做一次探测再回到固定节律。"""
        self._wake.set()

    def _run(self) -> None:
        """循环主体：等到下一次探测时机，已登录则探测。异常不得杀死线程。"""
        while not self._stop.is_set():
            self._wake.wait(self._settings.login_check_interval_s)
            if self._stop.is_set():
                break
            self._wake.clear()
            if not self._is_logged_in():
                continue
            try:
                self._probe()
            except Exception as exc:  # noqa: BLE001 - 兜底：单次探测异常不终止
                self._log(f"登录态探测异常：{exc!s}")

    def _probe(self) -> None:
        """执行一次探测并按结果处置。

        清会话在锁内完成（与登录动作互斥）；失效回调在锁外调用——它可能要
        停监控线程并 join，持锁等待会拖长锁占用。
        """
        with self._auth_lock:
            if self._stop.is_set():
                return
            result = self._api.check_login()
            if result is False:
                self._api.clear_session()
        if result is False:
            self._on_invalid()