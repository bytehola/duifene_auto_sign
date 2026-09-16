"""对分易 HTTP 接口封装（外部 I/O）。"""

from __future__ import annotations

import random
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

from .errors import AuthError, RateLimited
from .models import CheckInActivity, Course
from .rate_limit import RateLimiter
from .settings import Settings

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
STUDENT_REFERER = (
    "https://www.duifene.com/_CheckIn/MB/CheckInStudent.aspx?moduleid=16&pasd="
)
DISTANCE_RE = re.compile(r"距离[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*米")


class DuifeneApi:
    """持有注入的 `requests.Session`，负责所有网络往返与响应解析。

    签到类请求统一经 :meth:`_sign_post` 以施加限流约束。
    """

    def __init__(
        self,
        session: requests.Session,
        settings: Settings,
        limiter: RateLimiter,
    ) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._user_id: str | None = None

    @property
    def proxies(self) -> dict[str, str] | None:
        """底层会话使用的代理配置（供 WebSocket 复用）。"""
        return self._session.proxies or None

    @property
    def verify(self) -> bool:
        """底层会话的 TLS 校验开关（供 WebSocket 复用）。"""
        return bool(self._session.verify)

    @property
    def cached_user_id(self) -> str | None:
        """已缓存的学生 ID；未取过则为 None（不触发网络）。"""
        return self._user_id

    @property
    def limiter(self) -> RateLimiter:
        """限流状态机。"""
        return self._limiter

    def _url(self, path: str) -> str:
        """拼接完整 URL。"""
        return self._settings.host + path

    def clear_session(self) -> None:
        """清空 Cookie 并作废缓存的学生 ID。"""
        self._session.cookies.clear()
        self._user_id = None

    def login_by_link(self, link_code: str) -> requests.Response:
        """用微信回调 code 登录；新 Cookie 由 Set-Cookie 落到 session 的 cookie jar。"""
        self.clear_session()
        return self._session.get(
            self._url(f"/P.aspx?authtype=1&code={link_code}&state=1"), timeout=10
        )

    def login_by_password(self, username: str, password: str) -> str:
        """用账号密码登录；返回服务端 msgbox 文案，请求失败返回空串。"""
        headers = {**FORM_HEADERS, "Referer": f"{self._settings.host}/AppGate.aspx"}
        payload = f"action=loginmb&loginname={username}&password={password}"
        self.clear_session()
        self._session.get(self._settings.host, timeout=10)
        response = self._session.post(
            self._url("/AppCode/LoginInfo.ashx"),
            data=payload,
            headers=headers,
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return ""
        return response.json().get("msgbox", "")

    def fetch_courses(self) -> list[dict[str, Any]]:
        """拉取学生课程列表；请求失败返回空列表。

        Raises:
            AuthError: 接口以 msgbox 形式返回错误（通常为登录失效）。
        """
        headers = {
            **FORM_HEADERS,
            "Referer": f"{self._settings.host}/_UserCenter/PC/CenterStudent.aspx",
        }
        response = self._session.post(
            self._url("/_UserCenter/CourseInfo.ashx"),
            data="action=getstudentcourse&classtypeid=2",
            headers=headers,
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return []
        data = response.json()
        if isinstance(data, list):
            return data
        message = (
            data.get("msgbox", "登录失效") if isinstance(data, dict) else "登录失效"
        )
        raise AuthError(message)

    def check_login(self) -> bool | None:
        """检查登录态：True 已登录 / False 已失效 / None 网络失败（无法判定）。"""
        headers = {
            **FORM_HEADERS,
            "Referer": f"{self._settings.host}/_UserCenter/PC/CenterStudent.aspx",
        }
        response = self._session.get(
            self._url("/AppCode/LoginInfo.ashx"),
            data="Action=checklogin",
            headers=headers,
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return None
        return response.json().get("msg") == "1"

    def fetch_user_id(self) -> str | None:
        """获取并缓存学生 ID（会话内不变，取到后复用）。"""
        if self._user_id:
            return self._user_id
        response = self._session.get(
            self._url("/_UserCenter/MB/index.aspx"),
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return None
        element = BeautifulSoup(response.text, "html.parser").find(id="hidUID")
        if element is not None:
            self._user_id = element.get("value")
        return self._user_id

    def _require_user_id(self) -> str:
        """取学生 ID，取不到即抛错，绝不允许 ``None`` 拼入签到参数。

        轮询每轮开头已确认过 ID，这里防的是轮次中途会话被清理（登录失效
        竞态）后的空参数提交：``sid=None`` 会被服务端当作非法请求，返回
        文案还会被 ``_report`` 误判为普通失败。

        Raises:
            AuthError: 无法获取学生 ID，需重新登录。
        """
        user_id = self.fetch_user_id()
        if not user_id:
            raise AuthError("无法获取学生 ID，请重新登录")
        return user_id

    def fetch_activities(
        self, class_id: str, user_id: str
    ) -> list[CheckInActivity] | None:
        """拉取当日签到活动列表；接口异常返回 None。"""
        headers = {
            **FORM_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._settings.host}/_CheckIn/PC/StudentNoCheckCount.aspx",
        }
        response = self._session.post(
            self._url("/_CheckIn/MBCount.ashx"),
            data={
                "action": "getstudentinlogbyday",
                "classid": class_id,
                "studentid": user_id,
            },
            headers=headers,
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get("msg") != "1":
            return None
        return [CheckInActivity.from_raw(row) for row in data.get("rows", [])]

    def fetch_headcount(self, check_in_id: str) -> tuple[int, int] | None:
        """查询签到人数 `(已签到, 总人数)`；失败返回 None。

        已签到 = 总数 − 缺勤（迟到、请假亦视为已参与）。
        """
        headers = {
            **FORM_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._settings.host}/_CheckIn/PC/StudentNoCheckCount.aspx",
        }
        try:
            response = self._session.post(
                self._url("/_CheckIn/MBCount.ashx"),
                data={
                    "action": "getcheckintotalbyciid",
                    "ciid": check_in_id,
                    "t": "cking",
                },
                headers=headers,
                timeout=self._settings.request_timeout_s,
            )
            if response.status_code != 200:
                return None
            data = response.json()
            total = int(data.get("TotalNumber", 0))
            absence = int(data.get("AbsenceNumber", total))
            return max(total - absence, 0), total
        except (
            requests.exceptions.RequestException,
            TypeError,
            ValueError,
            AttributeError,
        ):
            return None

    def _sign_post(
        self, path: str, payload: str, timeout: float | None = None
    ) -> requests.Response:
        """发起签到类请求并施加限流约束。

        Raises:
            RateLimited: 处于静默期（不发送请求），或响应命中限流关键字
                （此时已进入静默）。
        """
        self._limiter.raise_if_blocking()
        response = self._session.post(
            self._url(path),
            data=payload,
            headers={**FORM_HEADERS, "Referer": STUDENT_REFERER},
            timeout=timeout or self._settings.request_timeout_s,
        )
        if self._limiter.is_limit_text(response.text):
            self._limiter.trip()
            raise RateLimited(
                f"服务端提示签到过于频繁，已静默 "
                f"{int(self._settings.cooldown_seconds)} 秒"
            )
        return response

    def sign_by_code(self, code: str) -> str | None:
        """提交 4 位签到码；异常见 :meth:`_sign_post` 与 :meth:`_require_user_id`。"""
        user_id = self._require_user_id()
        response = self._sign_post(
            "/_CheckIn/CheckIn.ashx",
            f"action=studentcheckin&studentid={user_id}&checkincode={code}",
        )
        if response.status_code != 200:
            return None
        return response.json().get("msgbox", "")

    def sign_by_qr(self, state: str) -> str:
        """以活动 ID 作为 state 完成二维码签到。

        二维码签到仅对微信授权登录的会话有效；账号密码登录调用时服务端返回
        语义含糊的「参数错误」，此处替换为准确提示。
        """
        response = self._session.get(
            self._url(f"/_CheckIn/MB/QrCodeCheckOK.aspx?state={state}"),
            timeout=self._settings.request_timeout_s,
        )
        if response.status_code != 200:
            return "二维码签到请求失败"
        element = BeautifulSoup(response.text, "html.parser").find(id="DivOK")
        text = element.get_text().strip() if element is not None else ""
        if text == "参数错误,请重新尝试":
            return "非微信链接登录，二维码无法签到"
        return text

    def sign_by_location(self, longitude: float, latitude: float) -> str | None:
        """定位签到：坐标做微小抖动后提交；异常见 :meth:`_sign_post`。"""
        jitter = self._settings.location_jitter_deg
        lon = round(longitude + random.uniform(-jitter, jitter), 8)
        lat = round(latitude + random.uniform(-jitter, jitter), 8)
        response = self._sign_post(
            "/_CheckIn/CheckInRoomHandler.ashx",
            f"action=signin&sid={self._require_user_id()}&longitude={lon}&latitude={lat}",
            timeout=15,
        )
        if response.status_code != 200:
            return None
        return response.json().get("msgbox", "")

    def probe_distance(
        self, course: Course, longitude: float, latitude: float
    ) -> float | None:
        """距离预言机：测该坐标到教室的距离；None 表示该点落在签到范围内
        （本次探测已真实签到）。RateLimited 时调用方应中止探测。"""
        response = self._sign_post(
            "/_CheckIn/CheckInRoomHandler.ashx",
            f"action=signin&cid={course.course_id}&tcid={course.class_id}"
            f"&sid={self._require_user_id()}&longitude={longitude}&latitude={latitude}",
            timeout=15,
        )
        if response.status_code != 200:
            return None
        match = DISTANCE_RE.search(response.text)
        return float(match.group(1)) if match else None
