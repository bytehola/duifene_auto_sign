"""学生签到门面：从个人记录发现候选活动，再读取活动详情。"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime

import requests

from .app_api import AppCheckIn, AppIdentity, DfAppApi, parse_checkin_time, CHINA_TZ
from .config import IniStore
from .credentials import CredentialStore, SavedCredentials
from .errors import AppApiError, PermissionDenied, RateLimited
from .models import Course
from .rate_limit import RateLimiter
from .settings import Settings
from .web_api import StudentWebApi


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
        self._operation_lock = threading.RLock()
        self._known_courses: set[tuple[str, str]] = set()
        self._credentials = CredentialStore(session_store) if session_store is not None else None
        self._login_credentials: SavedCredentials | None = None
        self._student_web = StudentWebApi(settings, self._web_credentials)

    def _web_credentials(self) -> SavedCredentials | None:
        identity = self.identity
        if identity is None or identity.user_type_id != "2":
            return None
        credentials = self._login_credentials
        if credentials is None and self._credentials is not None:
            credentials = self._credentials.load()
        return credentials if credentials is not None and credentials.account == identity.account else None

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
        with self._operation_lock:
            self._app_api.clear()
            self._student_web.clear()
            self._login_credentials = None
            self._known_courses.clear()

    def ensure_session_fresh(self, force: bool = False) -> bool:
        """保持 App 登录状态有效；返回本轮是否执行了刷新。"""
        return self._app_api.ensure_session_fresh(force=force)

    def login_by_password(self, username: str, password: str) -> str:
        """使用账号密码登录 App 接口。"""
        with self._operation_lock:
            self._known_courses.clear()
            self._student_web.clear()
            self._login_credentials = None
            if self._app_api.login(username, password):
                self._login_credentials = SavedCredentials(str(username).strip(), password)
                return "登录成功"
            return "登录失败，请检查账号、密码、网络或代理设置"

    def fetch_courses(self) -> list[Course]:
        """读取 App 接口中的课程与教学班。"""
        with self._operation_lock:
            courses = self._app_api.fetch_courses()
            self._known_courses = {(c.course_id, c.class_id) for c in courses}
            return courses

    def fetch_user_profile(self) -> tuple[str, str] | None:
        """返回当前账号与用户 ID。"""
        identity = self.identity
        if identity is None:
            return None
        return identity.account, identity.user_id

    def fetch_active_activities(
        self, course: Course, *, include_numeric_materials: bool = True,
    ) -> list[AppCheckIn]:
        """只处理当前学生已加入班级、个人记录中存在且仍有效的活动。"""
        with self._operation_lock:
            identity = self.identity
            if identity is None or identity.user_type_id != "2":
                raise PermissionDenied("监听需要当前学生身份，请登录学生账号")
            if (course.course_id, course.class_id) not in self._known_courses:
                raise PermissionDenied("所选课程不属于当前学生账号已加载的教学班")
            records = self._app_api.fetch_student_records(course.class_id)
            candidates = {}
            for row in records:
                if str(row.get("CheckInStatus")) == "1" or str(row.get("StatusID")) == "1":
                    continue
                try:
                    age = (datetime.now(CHINA_TZ) - parse_checkin_time(str(row.get("CreaterDate") or ""))).total_seconds()
                except ValueError as exc:
                    raise AppApiError("学生签到记录创建时间异常，未处理该轮活动") from exc
                # 保留现有“旧活动”配置；负年龄不当作新活动提交。
                if age < 0 or age > self._settings.stale_sign_minutes * 60:
                    continue
                candidates[str(row["ID"])] = row
            if not candidates:
                return []
            activities = []
            for activity_id, student in candidates.items():
                activity = self._app_api.fetch_checkin(activity_id)
                if activity is None:
                    raise AppApiError("已发现学生签到记录，但未取得活动详情")
                activity = replace(
                    activity, student_id=identity.user_id,
                    check_in_status=str(student.get("CheckInStatus") or ""),
                    status_id=str(student.get("StatusID") or ""),
                    status_name=str(student.get("StatusName") or ""),
                )
                if activity.activity_id != activity_id or activity.class_id != course.class_id:
                    raise PermissionDenied("签到详情的活动或教学班归属不匹配，已停止处理")
                if not activity.has_valid_times:
                    raise AppApiError("签到有效期数据异常，未处理该轮活动")
                if activity.is_deleted or activity.has_ended():
                    continue
                activities.append(activity)
            if include_numeric_materials:
                activities = self.refresh_numeric_materials(course, activities)
            # 网页入口以课程为单位。只在该课程恰有一个候选活动、且当前学生只加入
            # 其中一个教学班时补二维码，避免多活动/多班级情况下混用材料。
            classes = {class_id for course_id, class_id in self._known_courses if course_id == course.course_id}
            if len(activities) == 1 and len(classes) == 1:
                activity = activities[0]
                if activity.check_type == "2" and not activity.qr_token:
                    token = self._student_web.fetch_token(course.course_id)
                    if token:
                        fresh = self._app_api.fetch_checkin(activity.activity_id)
                        if (fresh is None or fresh.activity_id != activity.activity_id
                                or fresh.class_id != course.class_id or fresh.check_type != "2"):
                            raise PermissionDenied("二维码查询后活动归属或类型已变化，未提交")
                        if not fresh.has_valid_times or fresh.is_deleted or fresh.has_ended():
                            return []
                        activities[0] = replace(activity, qr_token=token,
                                                create_date=fresh.create_date, valid_date=fresh.valid_date)
            return activities

    def refresh_numeric_materials(self, course: Course, activities: list[AppCheckIn]) -> list[AppCheckIn]:
        """只刷新已确认活动的数字材料，不重复查询个人记录和 App 详情。"""
        with self._operation_lock:
            identity = self.identity
            if identity is None or identity.user_type_id != "2":
                raise PermissionDenied("取码需要当前学生身份")
            if (course.course_id, course.class_id) not in self._known_courses:
                raise PermissionDenied("取码课程不属于当前学生已加载的教学班")
            for activity in activities:
                if activity.student_id != identity.user_id or activity.class_id != course.class_id:
                    raise PermissionDenied("取码活动与当前学生或教学班不匹配，未提交")
                if not activity.has_valid_times:
                    raise AppApiError("取码活动有效期异常，未提交")
            activities = [a for a in activities if not a.is_deleted and not a.has_ended()
                          and not a.is_attended and a.age_seconds() is not None
                          and 0 <= a.age_seconds() <= self._settings.stale_sign_minutes * 60]
            missing_codes = {a.activity_id for a in activities if a.check_type == "1" and not (
                len(a.code) == 4 and a.code.isascii() and a.code.isdigit()
            )}
            if missing_codes:
                materials = self._student_web.fetch_numeric_materials(course.class_id, missing_codes)
                updated = []
                for activity in activities:
                    row = materials.get(activity.activity_id)
                    if row is not None:
                        if str(row.get("CheckInType")) != "1":
                            raise PermissionDenied("数字签到材料类型与活动不匹配，未提交")
                        if str(row.get("IsDel") or "0").lower() in ("1", "true"):
                            continue
                        try:
                            deadline = min(parse_checkin_time(activity.valid_date),
                                           parse_checkin_time(str(row.get("ValidDate") or "")))
                        except ValueError as exc:
                            raise AppApiError("数字签到材料有效期异常，未提交") from exc
                        if datetime.now(CHINA_TZ) >= deadline:
                            continue
                        code = row.get("CheckInCode")
                        if isinstance(code, str) and len(code) == 4 and code.isascii() and code.isdigit():
                            activity = replace(activity, code=code, valid_date=deadline.isoformat())
                    updated.append(activity)
                activities = updated
            return activities

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
