"""对分易 App REST 接口封装。

本模块是运行期唯一的认证与签到数据来源。账号登录、课程读取、用户信息、
当前签到、教室坐标和四类签到提交都使用 App 接口。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import quote

import requests

from .config import IniStore
from .errors import AppApiError, AuthError
from .models import Course
from .settings import Settings

ACCESS_TOKEN_LIFETIME_S = 30 * 60
REFRESH_TOKEN_LIFETIME_S = 7 * 24 * 60 * 60
TOKEN_REFRESH_AHEAD_S = 5 * 60


@dataclass(frozen=True)
class AppIdentity:
    """App 登录态中的当前用户。"""

    account: str
    user_id: str
    user_name: str
    user_type_id: str


@dataclass(frozen=True)
class AppCheckIn:
    """App 接口返回的当前签到。"""

    activity_id: str
    check_type: str
    code: str
    qr_token: str
    room_id: str
    class_id: str
    create_date: str
    valid_date: str
    check_in_status: str
    status_id: str
    status_name: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AppCheckIn":
        """从 ``GetModelByInProgress`` 或 ``GetCheckIn`` 响应构造活动。"""
        check_in = payload.get("checkIn")
        if not isinstance(check_in, Mapping):
            check_in = payload
        activity_id = str(check_in.get("id") or "")
        if not activity_id or activity_id == "0":
            raise ValueError("当前没有进行中的签到")
        return cls(
            activity_id=activity_id,
            check_type=str(check_in.get("checkInType") or ""),
            code=str(check_in.get("checkInCode") or "").strip(),
            qr_token=str(payload.get("qrCode") or "").strip(),
            room_id=str(check_in.get("roomID") or ""),
            class_id=str(check_in.get("tClassID") or ""),
            create_date=str(check_in.get("createrDate") or ""),
            valid_date=str(check_in.get("validDate") or ""),
            check_in_status=str(check_in.get("checkInStatus") or ""),
            status_id=str(check_in.get("statusID") or ""),
            status_name=str(check_in.get("statusName") or ""),
        )

    @property
    def is_attended(self) -> bool:
        """平台是否已经将该用户记为出勤。"""
        return (
            self.check_in_status == "1"
            or self.status_id == "1"
            or self.status_name == "出勤"
        )

    def has_ended(self, now: datetime | None = None) -> bool:
        """签到是否已超过有效期；时间格式异常时返回 False。"""
        try:
            valid_until = datetime.fromisoformat(self.valid_date)
        except ValueError:
            return False
        current = now or (
            datetime.now(valid_until.tzinfo)
            if valid_until.tzinfo is not None
            else datetime.now()
        )
        return current > valid_until

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """签到创建后经过的秒数；创建时间不可解析时返回 None。"""
        try:
            created_at = datetime.fromisoformat(self.create_date)
        except ValueError:
            return None
        current = now or (
            datetime.now(created_at.tzinfo)
            if created_at.tzinfo is not None
            else datetime.now()
        )
        return (current - created_at).total_seconds()


class DfAppApi:
    """持有 App Bearer Token，负责全部运行期 REST 请求。"""

    def __init__(
        self,
        session: requests.Session,
        settings: Settings,
        session_store: IniStore | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._session_store = session_store
        self._auth_lock = threading.RLock()
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at = 0.0
        self._refresh_expires_at = 0.0
        self._identity: AppIdentity | None = None
        self._restore_persisted_session()

    @property
    def is_authenticated(self) -> bool:
        """是否持有可用 App Token 和用户信息。"""
        with self._auth_lock:
            return bool(
                self._token
                and self._refresh_token
                and self._identity
                and self._refresh_expires_at > time.time()
            )

    @property
    def identity(self) -> AppIdentity | None:
        """当前 App 用户信息。"""
        return self._identity

    def clear(self) -> None:
        """清空 App 登录态。"""
        with self._auth_lock:
            self._clear_memory()
            if self._session_store is not None:
                self._session_store.clear_app_session()

    def _clear_memory(self) -> None:
        """只清空内存中的登录态。"""
        self._token = None
        self._refresh_token = None
        self._token_expires_at = 0.0
        self._refresh_expires_at = 0.0
        self._identity = None

    def login(self, username: str, password: str) -> bool:
        """账号密码登录 App 接口；失败返回 False。"""
        with self._auth_lock:
            self._clear_memory()
        try:
            response = self._session.post(
                self._url("/Auth/login"),
                json={"username": username, "password": password},
                headers={"Content-Type": "application/json"},
                timeout=self._settings.request_timeout_s,
            )
            payload = response.json()
        except (requests.exceptions.RequestException, ValueError):
            self._restore_persisted_session()
            return False
        if response.status_code != 200 or not isinstance(payload, Mapping):
            self._restore_persisted_session()
            return False
        if not self._store_login_payload(payload, initial_login=True):
            self._restore_persisted_session()
            return False
        if self._identity is None:
            self.clear()
            return False
        return True

    def fetch_courses(self) -> list[Course]:
        """读取当前用户课程及教学班。"""
        identity = self._require_identity()
        course_path = {
            "1": "/Course/GetTeacherCourseList",
            "2": "/Course/GetStudentCourseList",
            "3": "/Course/GetAssistantCourseList",
        }.get(identity.user_type_id, "/Course/GetStudentCourseList")
        payload = self._request("GET", course_path)
        self._require_success(payload, "未返回课程")
        data = payload.get("data")
        raw_courses = data.get("courses") if isinstance(data, Mapping) else None
        if not isinstance(raw_courses, list):
            raise AppApiError("课程数据格式异常")

        courses: list[Course] = []
        for raw_course in raw_courses:
            if not isinstance(raw_course, Mapping):
                continue
            course_id = str(raw_course.get("courseID") or "")
            course_name = str(raw_course.get("courseName") or "").strip()
            if not course_id or course_id == "0":
                continue
            classes = self._fetch_classes(course_id, identity.user_type_id)
            for class_id, class_name in classes:
                display_name = course_name
                if len(classes) > 1 and class_name:
                    display_name = f"{course_name} · {class_name}"
                courses.append(
                    Course(
                        course_id=course_id,
                        class_id=class_id,
                        name=display_name,
                    )
                )
        return courses

    def fetch_active_checkin(self, course_id: str) -> AppCheckIn | None:
        """读取课程当前进行中的签到；没有活动时返回 None。"""
        payload = self._request(
            "GET",
            f"/CK/GetModelByInProgress/{quote(str(course_id), safe='')}",
        )
        self._require_success(payload, "读取当前签到失败")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise AppApiError("当前签到数据格式异常")
        try:
            return AppCheckIn.from_payload(data)
        except ValueError:
            return None

    def fetch_checkin(self, activity_id: str) -> AppCheckIn | None:
        """按活动 ID 读取签到详情。"""
        payload = self._request(
            "GET",
            f"/CK/GetCheckIn/{quote(str(activity_id), safe='')}",
        )
        self._require_success(payload, "读取签到详情失败")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return None
        try:
            return AppCheckIn.from_payload(data)
        except ValueError:
            return None

    def fetch_room_coordinates(self, room_id: str) -> tuple[float, float]:
        """读取指定教室的经纬度。"""
        payload = self._request(
            "GET",
            f"/CK/room/GetCheckInRoomById/{quote(str(room_id), safe='')}",
        )
        self._require_success(payload, "读取教室坐标失败")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise AppApiError("教室坐标数据格式异常")
        try:
            return float(data["longitude"]), float(data["latitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppApiError("教室坐标数据格式异常") from exc

    def fetch_headcount(self, activity_id: str) -> tuple[int, int]:
        """读取签到人数 ``(已参与, 总人数)``。"""
        payload = self._request(
            "POST",
            "/CK/GetCheckInTotalByCheckInIds",
            json_data={"checkInIds": [int(activity_id)]},
        )
        self._require_success(payload, "读取签到人数失败")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise AppApiError("签到人数数据格式异常")
        try:
            total = int(data.get("totalNumber") or 0)
            absence = int(data.get("absenceNumber") or 0)
        except (TypeError, ValueError) as exc:
            raise AppApiError("签到人数数据格式异常") from exc
        return max(total - absence, 0), total

    def sign_by_code(self, code: str) -> str:
        """提交签到码或汉字答案。"""
        return self._sign("/CK/StudentCheckIn", {"checkInCode": code})

    def sign_by_qr(self, token: str) -> str:
        """提交二维码令牌。"""
        return self._sign("/CK/QrCodeCheckIn", {"checkInCode": token})

    def sign_by_location(self, longitude: float, latitude: float) -> str:
        """提交定位签到坐标。"""
        return self._sign(
            "/CK/CheckInSignInLocation",
            {"latitude": latitude, "longitude": longitude},
        )

    def _fetch_classes(
        self,
        course_id: str,
        user_type_id: str,
    ) -> list[tuple[str, str]]:
        """读取课程下当前用户可用的教学班。"""
        payload = self._request(
            "GET",
            (
                "/Class/GetClassListByCourseID/"
                f"{quote(str(course_id), safe='')}/"
                f"{quote(str(user_type_id), safe='')}"
            ),
        )
        self._require_success(payload, "未返回教学班")
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        classes: list[tuple[str, str]] = []
        for raw_class in data:
            if not isinstance(raw_class, Mapping):
                continue
            class_id = str(raw_class.get("classId") or "")
            if not class_id or class_id == "0":
                continue
            classes.append((class_id, str(raw_class.get("className") or "")))
        return classes

    def _store_login_payload(
        self,
        payload: Mapping[str, Any],
        *,
        initial_login: bool,
    ) -> bool:
        """保存登录或刷新响应中的 Token；格式不合法时返回 False。"""
        if not payload.get("success") or str(payload.get("code")) != "200":
            return False
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return False
        token = str(data.get("token") or "")
        if not token:
            return False
        with self._auth_lock:
            now = time.time()
            self._token = token
            self._token_expires_at = now + ACCESS_TOKEN_LIFETIME_S

            refreshed_token = str(data.get("refreshToken") or "")
            if refreshed_token:
                self._refresh_token = refreshed_token
                self._refresh_expires_at = now + REFRESH_TOKEN_LIFETIME_S
            elif initial_login or not self._refresh_token:
                return False

            user_info = data.get("userInfo")
            if isinstance(user_info, Mapping):
                user_id = str(user_info.get("userId") or "")
                account = str(user_info.get("userLogin") or "")
                if user_id and account:
                    self._identity = AppIdentity(
                        account=account,
                        user_id=user_id,
                        user_name=str(user_info.get("userName") or ""),
                        user_type_id=str(user_info.get("userTypeID") or ""),
                    )
            if self._identity is None or not self._refresh_token:
                return False
            self._save_session()
            return True

    def _restore_persisted_session(self) -> None:
        """从配置文件恢复尚未过期的 App 会话。"""
        if self._session_store is None:
            return
        values = self._session_store.load_app_session()
        if not values:
            return
        try:
            token_expires_at = float(values.get("access_expires_at") or 0)
            refresh_expires_at = float(values.get("refresh_expires_at") or 0)
        except ValueError:
            self._session_store.clear_app_session()
            return
        token = str(values.get("access_token") or "")
        refresh_token = str(values.get("refresh_token") or "")
        account = str(values.get("account") or "")
        user_id = str(values.get("user_id") or "")
        user_type_id = str(values.get("user_type_id") or "")
        if (
            not token
            or not refresh_token
            or not account
            or not user_id
            or not user_type_id
            or refresh_expires_at <= time.time()
        ):
            self._session_store.clear_app_session()
            return
        with self._auth_lock:
            self._token = token
            self._refresh_token = refresh_token
            self._token_expires_at = token_expires_at
            self._refresh_expires_at = refresh_expires_at
            self._identity = AppIdentity(
                account=account,
                user_id=user_id,
                user_name=str(values.get("user_name") or ""),
                user_type_id=user_type_id,
            )

    def _save_session(self) -> None:
        """把当前 App 会话写入本地配置。"""
        if (
            self._session_store is None
            or self._identity is None
            or not self._token
            or not self._refresh_token
        ):
            return
        self._session_store.save_app_session(
            {
                "account": self._identity.account,
                "user_id": self._identity.user_id,
                "user_name": self._identity.user_name,
                "user_type_id": self._identity.user_type_id,
                "access_token": self._token,
                "access_expires_at": f"{self._token_expires_at:.3f}",
                "refresh_token": self._refresh_token,
                "refresh_expires_at": f"{self._refresh_expires_at:.3f}",
            }
        )

    def _require_identity(self) -> AppIdentity:
        """返回登录用户；未登录时抛出认证错误。"""
        if self._identity is None:
            raise AuthError("登录状态已失效，请重新登录账号")
        return self._identity

    def _url(self, path: str) -> str:
        return self._settings.app_host.rstrip("/") + path

    def ensure_session_fresh(self, force: bool = False) -> bool:
        """在访问 Token 临近过期时刷新；返回本轮是否执行了刷新。"""
        with self._auth_lock:
            if (
                not self._token
                or not self._refresh_token
                or self._identity is None
                or self._refresh_expires_at <= time.time()
            ):
                self.clear()
                raise AuthError("登录状态已失效，请重新登录账号")
            refresh_deadline = self._token_expires_at - TOKEN_REFRESH_AHEAD_S
            if not force and time.time() < refresh_deadline:
                return False
            if not self._refresh_access_token():
                raise AuthError("登录状态已失效，请重新登录账号")
            return True

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Mapping[str, Any] | None = None,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        """发起带 Bearer Token 的请求。"""
        self.ensure_session_fresh()
        with self._auth_lock:
            request_token = self._token
        if not request_token:
            raise AuthError("登录状态已失效，请重新登录账号")
        try:
            response = self._session.request(
                method,
                self._url(path),
                json=json_data,
                headers={
                    "Authorization": f"Bearer {request_token}",
                    "Content-Type": "application/json",
                },
                timeout=self._settings.request_timeout_s,
            )
        except requests.exceptions.RequestException as exc:
            raise AppApiError(
                "网络请求失败，请检查网络或代理设置"
            ) from exc
        if response.status_code == 401 and allow_refresh:
            self.ensure_session_fresh(force=True)
            return self._request(
                method,
                path,
                json_data=json_data,
                allow_refresh=False,
            )
        if response.status_code == 401:
            self.clear()
            raise AuthError("登录状态已失效，请重新登录账号")
        if response.status_code != 200:
            raise AppApiError(f"请求失败（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppApiError("返回了无法解析的数据") from exc
        if not isinstance(payload, dict):
            raise AppApiError("返回了无效数据")
        return payload

    def _refresh_access_token(self) -> bool:
        """用 Refresh Token 换新 Token；仅服务端拒绝时清空登录态。"""
        with self._auth_lock:
            if (
                not self._token
                or not self._refresh_token
                or self._identity is None
                or self._refresh_expires_at <= time.time()
            ):
                self.clear()
                return False
            old_token = self._token
            refresh_token = self._refresh_token
            try:
                response = self._session.get(
                    self._url("/Auth/RefreshAccessToken"),
                    params={
                        "OldToken": old_token,
                        "RefreshToken": refresh_token,
                    },
                    headers={"Authorization": f"Bearer {old_token}"},
                    timeout=self._settings.request_timeout_s,
                )
            except requests.exceptions.RequestException as exc:
                raise AppApiError("登录状态续期失败，请检查网络或代理设置") from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise AppApiError("登录状态续期返回了无法解析的数据") from exc
            if response.status_code in (400, 401, 403):
                self.clear()
                return False
            if response.status_code != 200:
                raise AppApiError(
                    f"登录状态续期失败（HTTP {response.status_code}）"
                )
            if not isinstance(payload, Mapping):
                raise AppApiError("登录状态续期返回了无效数据")
            if not self._store_login_payload(payload, initial_login=False):
                self.clear()
                return False
            return True

    def _sign(self, path: str, data: Mapping[str, Any]) -> str:
        """提交签到并返回服务端文案。"""
        payload = self._request("POST", path, json_data=data)
        message = payload.get("message") or payload.get("msgbox")
        if message is None:
            if payload.get("success"):
                raise AppApiError("未返回签到结果")
            raise AppApiError("签到请求失败")
        return str(message)

    @staticmethod
    def _require_success(payload: Mapping[str, Any], fallback: str) -> None:
        """接口响应失败时抛出带服务端原因的异常。"""
        if payload.get("success"):
            return
        message = str(payload.get("message") or "").strip()
        raise AppApiError(message or fallback)
