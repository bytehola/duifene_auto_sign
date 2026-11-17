"""权限拒绝必须停止监听，且不能误清理仍然有效的登录态。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from duifene.api import DuifeneApi
from duifene.app_api import AppCheckIn, AppIdentity, DfAppApi
from duifene.config import IniStore
from duifene.credentials import SavedCredentials
from duifene.errors import AppApiError, AuthError, PermissionDenied
from duifene.models import Course
from duifene.rate_limit import RateLimiter
from duifene.settings import Settings
from duifene.watcher import Watcher


def response(status, payload=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode("utf-8") if payload else b""
    return result


def plain_response(status, text):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = "text/plain; charset=utf-8"
    result._content = text.encode("utf-8")
    return result


class PermissionApiTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(spec=requests.Session)
        self.api = DfAppApi(self.session, Settings())
        self.api._store_login_payload(
            {
                "success": True,
                "code": 200,
                "data": {
                    "token": "test-access",
                    "refreshToken": "test-refresh",
                    "userInfo": {
                        "userId": "test-user",
                        "userLogin": "test-account",
                        "userTypeID": "2",
                    },
                },
            },
            initial_login=True,
        )

    def test_http_and_business_denials_preserve_session_without_refresh(self):
        for status, payload in (
            (403, None),
            (200, {"success": False, "code": 403, "message": "没有权限访问！"}),
            (200, {"success": False, "code": "403", "message": "没有权限访问！"}),
        ):
            with self.subTest(status=status, payload=payload):
                self.session.reset_mock()
                self.session.request.return_value = response(status, payload)
                with self.assertRaises(PermissionDenied) as caught:
                    self.api.fetch_active_checkin("private-record-id")
                message = str(caught.exception)
                self.assertIn("/CK/GetModelByInProgress", message)
                self.assertNotIn("private-record-id", message)
                self.assertTrue(self.api.is_authenticated)
                self.assertEqual(self.api._token, "test-access")
                self.session.request.assert_called_once()
                self.session.get.assert_not_called()

    def test_successful_empty_activity_remains_empty(self):
        self.session.request.return_value = response(
            200, {"success": True, "code": 200, "data": {"checkIn": {"id": 0}}}
        )
        self.assertIsNone(self.api.fetch_active_checkin("test-course"))

    def test_transient_http_failure_is_not_permission_denied(self):
        self.session.request.return_value = response(503)
        with self.assertRaises(AppApiError) as caught:
            self.api.fetch_active_checkin("test-course")
        self.assertNotIsInstance(caught.exception, PermissionDenied)
        self.assertTrue(self.api.is_authenticated)

    def test_refresh_401_plain_text_invalidates_session(self):
        self.session.get.return_value = plain_response(
            401,
            "登录已过期，请重新登录",
        )
        with self.assertRaises(AuthError):
            self.api.ensure_session_fresh(force=True)
        self.assertFalse(self.api.is_authenticated)
        self.session.get.assert_called_once()

    def test_expired_refresh_token_relogs_with_saved_credentials(self):
        settings = Settings()
        session = Mock(spec=requests.Session)
        api = DuifeneApi(session, settings, RateLimiter(settings))
        api._app_api._store_login_payload(
            {
                "success": True,
                "code": 200,
                "data": {
                    "token": "old-access",
                    "refreshToken": "expired-refresh",
                    "userInfo": {
                        "userId": "test-user",
                        "userLogin": "test-account",
                        "userTypeID": "2",
                    },
                },
            },
            initial_login=True,
        )
        api._login_credentials = SavedCredentials("test-account", "test-password")
        api._app_api._token_expires_at = 0
        session.get.return_value = plain_response(
            401,
            "登录已过期，请重新登录",
        )
        session.post.return_value = response(
            200,
            {
                "success": True,
                "code": 200,
                "data": {
                    "token": "new-access",
                    "refreshToken": "new-refresh",
                    "userInfo": {
                        "userId": "test-user",
                        "userLogin": "test-account",
                        "userTypeID": "2",
                    },
                },
            },
        )
        self.assertTrue(api.ensure_session_fresh(force=True))
        self.assertEqual(api._app_api._token, "new-access")
        self.assertEqual(api.identity.account, "test-account")
        session.post.assert_called_once()

    def test_login_registers_dual_identity_and_switches_teacher_to_student(self):
        settings = Settings()
        session = Mock(spec=requests.Session)
        api = DuifeneApi(session, settings, RateLimiter(settings))
        identities = iter([
            AppIdentity("test-account", "test-user", "", "1", "2"),
            AppIdentity("test-account", "test-user", "", "1", "3"),
            AppIdentity("test-account", "test-user", "", "2", "3"),
        ])

        def login_result(*_args):
            api._app_api._identity = next(identities)
            return True

        api._app_api.login = Mock(side_effect=login_result)
        api._app_api.register_double_user_type = Mock(return_value="申请成功")
        api._student_web.switch_to_student = Mock(return_value="切换为学生身份")

        self.assertEqual(
            api.login_by_password("test-account", "test-password"),
            "登录成功",
        )
        api._app_api.register_double_user_type.assert_called_once_with()
        api._student_web.switch_to_student.assert_called_once_with()
        self.assertEqual(api._app_api.login.call_count, 3)
        self.assertEqual(api.identity.user_type_id, "2")
        self.assertEqual(api.identity.final_user_type_id, "3")

    def test_room_coordinates_are_persisted_and_restored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            settings.ini_filename = str(Path(temp_dir) / "duifenyi.ini")
            store = IniStore(settings.ini_filename)
            store.ensure_exists()
            activity = AppCheckIn.from_payload({
                "id": "101",
                "tClassID": "202",
                "checkInType": "3",
                "roomID": "4499883696309602",
                "createrDate": "2026/11/18 10:00:00",
                "validDate": "2026/11/18 10:10:00",
            })

            first_api = DuifeneApi(
                Mock(spec=requests.Session),
                settings,
                RateLimiter(settings),
                store,
            )
            first_api._app_api.fetch_room_coordinates = Mock(
                return_value=(104.0668, 30.5728)
            )
            self.assertEqual(
                first_api.fetch_room_coordinates(activity),
                (104.0668, 30.5728),
            )
            self.assertEqual(
                store.load_room_coordinates(),
                {"4499883696309602": (104.0668, 30.5728)},
            )

            restored_api = DuifeneApi(
                Mock(spec=requests.Session),
                settings,
                RateLimiter(settings),
                store,
            )
            restored_api._app_api.fetch_room_coordinates = Mock()
            self.assertEqual(
                restored_api.fetch_room_coordinates(activity),
                (104.0668, 30.5728),
            )
            restored_api._app_api.fetch_room_coordinates.assert_not_called()

    def test_invalid_persisted_room_coordinates_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "duifenyi.ini"
            ini_path.write_text(
                "[ROOM_COORDINATES]\n"
                "0=104.0,30.0\n"
                "bad=104.0,30.0\n"
                "101=0,0\n"
                "102=181,30\n"
                "103=104.0,30.0\n",
                encoding="utf-8",
            )
            store = IniStore(str(ini_path))
            self.assertEqual(
                store.load_room_coordinates(),
                {"103": (104.0, 30.0)},
            )


class PermissionWatcherTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.app_authenticated = True
        self.processor = Mock()
        self.logs = []
        self.watcher = Watcher(Settings(), self.api, self.processor, self.logs.append)
        self.watcher.set_course(Course("test-course", "test-class", "测试课程"))
        self.watcher._in_monitor_window = Mock(return_value=True)

    def test_permission_denied_stops_after_one_request_without_signing(self):
        self.api.fetch_active_activities.side_effect = PermissionDenied("无访问权限（403）")
        self.watcher._sleep = Mock()
        self.watcher.start()
        self.watcher._thread.join(timeout=2)
        self.assertFalse(self.watcher.is_running)
        self.api.fetch_active_activities.assert_called_once_with(Course("test-course", "test-class", "测试课程"), include_numeric_materials=False)
        self.processor.process.assert_not_called()
        self.api.clear_session.assert_not_called()
        self.watcher._sleep.assert_not_called()
        self.assertTrue(any("监听已停止" in line and "403" in line for line in self.logs))
        self.assertFalse(any("后重试" in line for line in self.logs))

    def test_auth_error_notifies_recovery_callback(self):
        self.api.fetch_active_activities.side_effect = AuthError("登录状态已失效")
        recovery_callback = Mock()
        self.watcher._on_auth_lost = recovery_callback
        self.watcher._sleep = Mock()

        self.watcher.start()
        self.watcher._thread.join(timeout=2)

        self.assertFalse(self.watcher.is_running)
        recovery_callback.assert_called_once_with()

    def test_network_failure_still_retries_and_recovers(self):
        self.api.fetch_active_activities.side_effect = [AppApiError("网络请求失败"), []]

        def sleep(_seconds):
            if self.api.fetch_active_activities.call_count == 2:
                self.watcher._stop.set()

        self.watcher._sleep = sleep
        self.watcher._run()
        self.assertEqual(self.api.fetch_active_activities.call_count, 2)
        self.assertTrue(any("后重试" in line for line in self.logs))
        self.api.clear_session.assert_not_called()
        self.processor.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
