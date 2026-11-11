"""领域异常与日志回调类型。"""

from __future__ import annotations

from typing import Callable

LogFn = Callable[[str], None]


class AuthError(Exception):
    """登录态失效，需要重新登录。"""


class AppApiError(Exception):
    """App 接口请求失败，当前轮次无法继续。"""


class PermissionDenied(AppApiError):
    """服务端拒绝当前账号访问接口，重试不能恢复权限。"""


class WebSessionError(AppApiError):
    """学生网页会话无法恢复，需用户处理；App 登录态应保留。"""


class RateLimited(Exception):
    """签到接口返回限流，已进入静默期。"""
