"""签到限流状态机。"""

from __future__ import annotations

import time

from .errors import LogFn, RateLimited
from .settings import Settings


class RateLimiter:
    """签到限流状态机。

    服务端以「最后一次签到请求」为起点计时，期间任何签到请求都会重置它，
    因此静默期内必须完全不发请求，靠轮询探测解除是无效的。
    """

    def __init__(self, settings: Settings, log: LogFn | None = None) -> None:
        """log 在进入静默时告警一次，作为用户可见的倒计时起点。"""
        self._settings = settings
        self._log = log or (lambda _message: None)
        self._until = 0.0

    def remaining(self, now: float | None = None) -> float:
        """距解除还剩的秒数；<=0 表示未处于限流。"""
        return max(0.0, self._until - (time.time() if now is None else now))

    def is_blocking(self) -> bool:
        """当前是否处于静默期。"""
        return self.remaining() > 0

    def raise_if_blocking(self) -> None:
        """静默期内调用即抛 :class:`RateLimited`，调用方据此保证不发请求。"""
        left = self.remaining()
        if left > 0:
            raise RateLimited(f"限流静默中，还需 {int(left)} 秒才恢复")

    def trip(self) -> None:
        """进入静默，开始 `cooldown_seconds` 计时。

        仅在尚未静默时记日志：静默期内不会有新请求，故不会重复触发。
        """
        if self.remaining() > 0:
            return
        self._until = time.time() + self._settings.cooldown_seconds
        self._log(
            f"服务端提示签到过于频繁，已进入静默，"
            f"{int(self._settings.cooldown_seconds)} 秒后恢复"
        )

    def is_limit_text(self, text: str) -> bool:
        """响应文本是否命中限流关键字。"""
        return any(marker in text for marker in self._settings.rate_limit_markers)
