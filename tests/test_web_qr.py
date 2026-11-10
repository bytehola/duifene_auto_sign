"""学生二维码来源、解码和网页会话恢复回归，不访问网络。"""

import base64
import io
import json
import unittest
from unittest.mock import Mock

import requests
import zxingcpp
from PIL import Image

from duifene.credentials import SavedCredentials
from duifene.errors import AppApiError, PermissionDenied, WebSessionError
from duifene.settings import Settings
from duifene.web_api import StudentWebApi, decode_qr_token

TOKEN = "A" * 48


def encoded_qr(url="https://www.duifene.com/G.aspx?OnlyId=2&e=" + TOKEN):
    matrix = zxingcpp.create_barcode(url, zxingcpp.BarcodeFormat.QRCode).to_image(scale=4)
    image = Image.frombytes("L", (matrix.shape[1], matrix.shape[0]), bytes(matrix))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def response(payload=None, status=200, text=None):
    result = requests.Response()
    result.status_code = status
    result._content = (json.dumps(payload) if text is None else text).encode("utf-8")
    return result


class QrDecodeTests(unittest.TestCase):
    def test_official_qr_decodes_without_visiting_link(self):
        self.assertEqual(decode_qr_token(encoded_qr()), TOKEN)

    def test_foreign_host_bad_type_and_duplicate_parameters_are_rejected(self):
        for url in ("https://example.com/G.aspx?OnlyId=2&e=" + TOKEN,
                    "https://www.duifene.com/G.aspx?OnlyId=1&e=" + TOKEN,
                    "https://www.duifene.com/G.aspx?OnlyId=2&e=" + TOKEN + "&e=" + TOKEN,
                    "https://www.duifene.com/G.aspx?OnlyId=2&e=bad",
                    "https://www.duifene.com/G.aspx?OnlyId=2&e=" + TOKEN + "#other"):
            with self.subTest(url=url), self.assertRaises(AppApiError):
                decode_qr_token(encoded_qr(url))

    def test_bad_base64_and_nonimage_are_rejected(self):
        for value in (None, "not base64!", base64.b64encode(b"not an image").decode(), "A" * 2_000_001):
            with self.subTest(kind=type(value)), self.assertRaises(AppApiError):
                decode_qr_token(value)


class WebQrTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(spec=requests.Session)
        self.session.cookies = requests.cookies.RequestsCookieJar()
        self.web = StudentWebApi(Settings(), lambda: SavedCredentials("student", "test&password"), self.session)
        self.login = [response(text="home"), response({"msgbox": "登录成功"})]
        self.qr = response({"msg": 1, "data": encoded_qr()})

    def test_normal_student_login_and_known_course_only(self):
        self.session.request.side_effect = self.login + [self.qr]
        self.assertEqual(self.web.fetch_token("course"), TOKEN)
        calls = self.session.request.call_args_list
        self.assertEqual(calls[1].kwargs["data"], {"action": "loginmb", "loginname": "student", "password": "test&password"})
        self.assertEqual(calls[-1].kwargs["data"], {"action": "getcodeimage", "cid": "course"})
        self.assertFalse(calls[-1].kwargs["allow_redirects"])
        self.assertFalse(self.session.trust_env)

    def test_expired_cookie_recovers_once(self):
        self.session.request.side_effect = self.login + [response(status=401)] + self.login + [self.qr]
        self.assertEqual(self.web.fetch_token("course"), TOKEN)
        self.assertEqual(self.session.request.call_count, 6)

    def test_repeated_login_page_stops_recovery(self):
        expired = response(text='<input id="topLogin_tbLoginName">')
        self.session.request.side_effect = self.login + [expired] + self.login + [expired]
        with self.assertRaises(WebSessionError):
            self.web.fetch_token("course")
        self.assertEqual(self.session.request.call_count, 6)

    def test_permission_error_does_not_relogin_or_change_role(self):
        self.session.request.side_effect = self.login + [response(status=403)]
        with self.assertRaises(PermissionDenied):
            self.web.fetch_token("course")
        self.assertEqual(self.session.request.call_count, 3)

    def test_activity_ended_while_querying_returns_no_material(self):
        self.session.request.side_effect = self.login + [response({"msg": -1, "msgbox": "没有正在签到"})]
        self.assertEqual(self.web.fetch_token("course"), "")

    def test_missing_credentials_does_not_send_requests(self):
        self.web._credentials = lambda: None
        with self.assertRaises(WebSessionError):
            self.web.fetch_token("course")
        self.session.request.assert_not_called()


class NumericWebTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(spec=requests.Session)
        self.session.cookies = requests.cookies.RequestsCookieJar()
        self.web = StudentWebApi(Settings(), lambda: SavedCredentials("student", "test"), self.session)
        self.login = [response(text="home"), response({"msgbox": "登录成功"})]
        self.row = {"ID": "101", "TClassID": "class", "CheckInCode": "0073"}

    def test_only_requested_activity_from_joined_class_is_returned(self):
        self.session.request.side_effect = self.login + [response({"rows": [
            {**self.row, "ID": "100"}, self.row]})]
        rows = self.web.fetch_numeric_materials("class", {"101"})
        self.assertEqual(set(rows), {"101"})
        self.assertEqual(rows["101"]["CheckInCode"], "0073")
        self.assertEqual(self.session.request.call_args.kwargs["data"], {
            "action": "gettclasscheckinlistbypage", "classid": "class", "page": "1", "nums": "10"})

    def test_foreign_class_material_is_rejected(self):
        self.session.request.side_effect = self.login + [response({"rows": [{**self.row, "TClassID": "other"}]})]
        with self.assertRaises(PermissionDenied):
            self.web.fetch_numeric_materials("class", {"101"})

    def test_missing_rows_is_not_misread_as_no_code(self):
        self.session.request.side_effect = self.login + [response({"total": 0})]
        with self.assertRaises(AppApiError):
            self.web.fetch_numeric_materials("class", {"101"})

    def test_duplicate_activity_material_is_rejected(self):
        self.session.request.side_effect = self.login + [response({"rows": [self.row, self.row]})]
        with self.assertRaises(AppApiError):
            self.web.fetch_numeric_materials("class", {"101"})

    def test_empty_candidates_do_not_login_or_send_query(self):
        self.assertEqual(self.web.fetch_numeric_materials("class", set()), {})
        self.session.request.assert_not_called()

    def test_expired_numeric_web_session_is_recovered_once(self):
        self.session.request.side_effect = self.login + [response(status=401)] + self.login + [response({"rows": [self.row]})]
        self.assertIn("101", self.web.fetch_numeric_materials("class", {"101"}))
        self.assertEqual(self.session.request.call_count, 6)


if __name__ == "__main__":
    unittest.main()
