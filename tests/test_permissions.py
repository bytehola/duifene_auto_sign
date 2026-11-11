"""权限拒绝必须停止监听，且不能误清理仍然有效的登录态。"""

import json
import unittest
from unittest.mock import Mock

import requests

from duifene.app_api import DfAppApi
from duifene.errors import AppApiError, PermissionDenied
from duifene.models import Course
from duifene.settings import Settings
from duifene.watcher import Watcher


def response(status, payload=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode("utf-8") if payload else b""
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
