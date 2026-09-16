"""领域异常与日志回调类型。"""

from __future__ import annotations

from typing import Callable

LogFn = Callable[[str], None]


class AuthError(Exception):
    """登录态失效，需要重新登录。"""


class RateLimited(Exception):
    """签到接口返回限流，已进入静默期。"""
