"""用当前学生的独立网页会话读取签到材料，不读取教师会话。"""

from __future__ import annotations

import base64
import binascii
import io
import re
import threading
from typing import Any, Callable, Mapping, TypeVar
from urllib.parse import parse_qs, urlparse

import requests
import zxingcpp
from PIL import Image, UnidentifiedImageError

from .credentials import SavedCredentials
from .errors import AppApiError, PermissionDenied, WebSessionError
from .settings import Settings

WEB_HOST = "https://www.duifene.com"
T = TypeVar("T")


def decode_qr_token(encoded: str) -> str:
    """只接受本次已验证的官方二维码格式；不访问二维码里的链接。"""
    if not isinstance(encoded, str) or len(encoded) > 2_000_000:
        raise AppApiError("网页二维码图片数据异常")
    try:
        data = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 4_000_000:
                raise AppApiError("网页二维码图片尺寸异常")
            codes = zxingcpp.read_barcodes(image.convert("RGB"), formats=zxingcpp.BarcodeFormat.QRCode)
    except (binascii.Error, ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AppApiError("网页二维码图片无法解析") from exc
    if len(codes) != 1:
        raise AppApiError("网页二维码未识别到唯一有效内容")
    parsed = urlparse(codes[0].text)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "www.duifene.com"
            or parsed.path != "/G.aspx" or parsed.fragment
            or set(query) != {"OnlyId", "e"} or query["OnlyId"] != ["2"]
            or len(query["e"]) != 1 or not re.fullmatch(r"[0-9A-Fa-f]{48}", query["e"][0])):
        raise AppApiError("网页二维码格式不受支持，未提交")
    return query["e"][0]


class _Expired(Exception):
    pass


class StudentWebApi:
    """Cookie 仅留在内存；网页登录最多恢复一次，不更改 App Token。"""

    def __init__(self, settings: Settings, credentials: Callable[[], SavedCredentials | None],
                 session: requests.Session | None = None) -> None:
        self._settings = settings
        self._credentials = credentials
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
        self._logged_in = False
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self._logged_in = False
            self._session.cookies.clear()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        self._session.verify = self._settings.verify_tls
        self._session.proxies = self._settings.proxies or {}
        try:
            return self._session.request(method, WEB_HOST + path, allow_redirects=False,
                                         timeout=self._settings.request_timeout_s, **kwargs)
        except requests.RequestException as exc:
            raise AppApiError("学生网页签到材料查询网络失败") from exc

    def _login(self) -> None:
        credentials = self._credentials()
        if credentials is None:
            raise WebSessionError("缺少当前学生账号的网页登录凭据，请重新登录学生账号")
        self.clear()
        first = self._request("GET", "/Home.aspx")
        if first.status_code != 200:
            raise AppApiError(f"学生网页登录入口请求失败（HTTP {first.status_code}）")
        result = self._request("POST", "/AppCode/LoginInfo.ashx",
                               data={"action": "loginmb", "loginname": credentials.account,
                                     "password": credentials.password},
                               headers={"Referer": WEB_HOST + "/AppGate.aspx"})
        try:
            payload = result.json()
        except ValueError as exc:
            raise WebSessionError("学生网页登录返回异常，请重新登录学生账号") from exc
        if result.status_code != 200 or not isinstance(payload, dict) or payload.get("msgbox") != "登录成功":
            raise WebSessionError("学生网页登录失败，请检查保存的凭据或重新登录学生账号")
        self._logged_in = True

    def _image(self, course_id: str) -> str:
        result = self._request("POST", "/_CheckIn/CheckIn.ashx",
                               data={"action": "getcodeimage", "cid": course_id},
                               headers={"Referer": WEB_HOST + "/_CheckIn/PC/TeachCheckIn.aspx"})
        if result.status_code in (301, 302, 303, 307, 308, 401):
            raise _Expired()
        if result.status_code == 403:
            raise PermissionDenied("当前学生账号无权读取该课程的二维码（403）")
        if result.status_code != 200:
            raise AppApiError(f"网页二维码请求失败（HTTP {result.status_code}）")
        try:
            payload = result.json()
        except ValueError as exc:
            if "topLogin_tbLoginName" in result.text:
                raise _Expired() from exc
            raise AppApiError("网页二维码返回了无法解析的数据") from exc
        if not isinstance(payload, dict):
            raise AppApiError("网页二维码返回数据格式异常")
        message = str(payload.get("msgbox") or "")
        if str(payload.get("code")) == "403" or "权限" in message:
            raise PermissionDenied("当前学生账号无权读取该课程的二维码（403）")
        if "登录" in message:
            raise _Expired()
        if str(payload.get("msg")) != "1":
            return ""
        return decode_qr_token(payload.get("data"))

    def _numeric_materials(self, class_id: str, activity_ids: set[str]) -> dict[str, Mapping[str, Any]]:
        result = self._request("POST", "/_CheckIn/MBCount.ashx",
                               data={"action": "gettclasscheckinlistbypage", "classid": class_id,
                                     "page": "1", "nums": "10"})
        if result.status_code in (301, 302, 303, 307, 308, 401):
            raise _Expired()
        if result.status_code == 403:
            raise PermissionDenied("当前学生账号无权读取该教学班的签到材料（403）")
        if result.status_code != 200:
            raise AppApiError(f"学生网页签到材料请求失败（HTTP {result.status_code}）")
        try:
            payload = result.json()
        except ValueError as exc:
            if "topLogin_tbLoginName" in result.text:
                raise _Expired() from exc
            raise AppApiError("网页签到材料返回了无法解析的数据") from exc
        if not isinstance(payload, dict):
            raise AppApiError("网页签到材料格式异常")
        message = str(payload.get("msgbox") or payload.get("message") or "")
        if str(payload.get("code")) == "403" or "权限" in message:
            raise PermissionDenied("当前学生账号无权读取该教学班的签到材料（403）")
        if "登录" in message:
            raise _Expired()
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise AppApiError("网页签到材料未返回记录列表")
        matches = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise AppApiError("网页签到材料条目格式异常")
            if str(row.get("TClassID") or "") != class_id:
                raise PermissionDenied("网页签到材料的教学班归属不匹配，未提交")
            activity_id = str(row.get("ID") or "")
            if activity_id in activity_ids:
                if activity_id in matches:
                    raise AppApiError("网页签到材料包含重复活动，未提交")
                matches[activity_id] = row
        return matches

    def _with_login(self, operation: Callable[[], T]) -> T:
        with self._lock:
            for attempt in range(2):
                if not self._logged_in:
                    self._login()
                try:
                    return operation()
                except _Expired:
                    self.clear()
                    if attempt:
                        raise WebSessionError("学生网页会话无法恢复，请重新登录学生账号") from None
        raise WebSessionError("学生网页会话无法恢复，请重新登录学生账号")

    def fetch_token(self, course_id: str) -> str:
        return self._with_login(lambda: self._image(course_id))

    def fetch_numeric_materials(self, class_id: str, activity_ids: set[str]) -> dict[str, Mapping[str, Any]]:
        if not activity_ids:
            return {}
        return self._with_login(lambda: self._numeric_materials(class_id, activity_ids))
