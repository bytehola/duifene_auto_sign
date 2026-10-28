"""对分易 App 接口门面。

业务层只依赖本类；登录、课程、签到数据和提交均转发到 :class:`DfAppApi`。
"""

from __future__ import annotations

import requests

from .app_api import AppCheckIn, AppIdentity, DfAppApi
from .config import IniStore
from .errors import RateLimited
from .models import Course
from .rate_limit import RateLimiter
from .settings import Settings


class DuifeneApi:
    """持有共享 HTTP 会话，向上层提供 App 接口能力。"""

    def __init__(
        self,
        session: requests.Session,
        settings: Settings,
        limiter: RateLimiter,
        session_store: IniStore | None = None,
    ) -> None:
        self._settings = settings
        self._limiter = limiter
        self._app_api = DfAppApi(session, settings, session_store)

    @property
    def app_authenticated(self) -> bool:
        """当前是否持有完整 App 登录态。"""
        return self._app_api.is_authenticated

    @property
    def identity(self) -> AppIdentity | None:
        """当前 App 登录用户。"""
        return self._app_api.identity

    @property
    def limiter(self) -> RateLimiter:
        """签到限流状态机。"""
        return self._limiter

    def clear_session(self) -> None:
        """清空 App 登录态。"""
        self._app_api.clear()

    def ensure_session_fresh(self, force: bool = False) -> bool:
        """保持 App 登录状态有效；返回本轮是否执行了刷新。"""
        return self._app_api.ensure_session_fresh(force=force)

    def login_by_password(self, username: str, password: str) -> str:
        """使用账号密码登录 App 接口。"""
        if self._app_api.login(username, password):
            return "登录成功"
        return "登录失败，请检查账号、密码、网络或代理设置"

    def fetch_courses(self) -> list[Course]:
        """读取 App 接口中的课程与教学班。"""
        return self._app_api.fetch_courses()

    def fetch_user_profile(self) -> tuple[str, str] | None:
        """返回当前账号与用户 ID。"""
        identity = self.identity
        if identity is None:
            return None
        return identity.account, identity.user_id

    def fetch_active_activity(self, course_id: str) -> AppCheckIn | None:
        """读取课程当前签到。"""
        return self._app_api.fetch_active_checkin(course_id)

    def fetch_checkin(self, activity_id: str) -> AppCheckIn | None:
        """读取指定签到详情。"""
        return self._app_api.fetch_checkin(activity_id)

    def fetch_headcount(self, activity_id: str) -> tuple[int, int]:
        """读取签到人数 ``(已参与, 总人数)``。"""
        return self._app_api.fetch_headcount(activity_id)

    def fetch_room_coordinates(
        self,
        activity: AppCheckIn,
    ) -> tuple[float, float] | None:
        """读取签到绑定教室的坐标。"""
        room_id = activity.room_id
        if not room_id or room_id == "0":
            detail = self.fetch_checkin(activity.activity_id)
            room_id = detail.room_id if detail is not None else ""
        if not room_id or room_id == "0":
            return None
        return self._app_api.fetch_room_coordinates(room_id)

    def sign_by_code(self, code: str) -> str:
        """提交签到码或汉字答案。"""
        return self._guard_rate_limit(self._app_api.sign_by_code(code))

    def sign_by_qr(self, token: str) -> str:
        """提交二维码令牌。"""
        return self._guard_rate_limit(self._app_api.sign_by_qr(token))

    def sign_by_location(self, longitude: float, latitude: float) -> str:
        """提交定位签到坐标。"""
        return self._guard_rate_limit(
            self._app_api.sign_by_location(longitude, latitude)
        )

    def _guard_rate_limit(self, message: str) -> str:
        """把 App 服务端的限流文案同步到本地静默状态。"""
        if self._limiter.is_limit_text(message):
            self._limiter.trip()
            raise RateLimited(
                f"服务端提示签到过于频繁，已静默 "
                f"{int(self._settings.cooldown_seconds)} 秒"
            )
        return message
