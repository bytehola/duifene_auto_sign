"""学生身份的多活动发现、取码和提交边界回归；不访问真实网络。"""

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import requests

from duifene.api import DuifeneApi
from duifene.app_api import AppCheckIn, AppIdentity, CHINA_TZ
from duifene.credentials import SavedCredentials
from duifene.errors import AppApiError, AuthError, PermissionDenied, WebSessionError
from duifene.models import Course, Outcome
from duifene.processor import ActivityProcessor
from duifene.rate_limit import RateLimiter
from duifene.settings import Settings
from duifene.watcher import Watcher


def response(payload=None, status=200, url="https://www.duifene.com/_CheckIn/MBCount.ashx", text=None):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = (text if text is not None else json.dumps(payload)).encode("utf-8")
    return r


def record(activity_id="101", **patch):
    now = datetime.now(CHINA_TZ)
    value = {"ID": activity_id, "StudentID": "self", "StatusID": "2", "StatusName": "缺勤",
             "CheckInType": "1", "CheckInStatus": "", "CreaterDate": (now - timedelta(seconds=60)).strftime("%Y/%m/%d %H:%M:%S")}
    value.update(patch)
    return value


def class_row(activity_id="101", **patch):
    now = datetime.now(CHINA_TZ)
    value = {"ID": activity_id, "TClassID": "class", "CheckInCode": "0073", "CheckInType": "1",
             "CreaterDate": (now - timedelta(seconds=60)).strftime("%Y/%m/%d %H:%M:%S"),
             "ValidDate": (now + timedelta(minutes=5)).strftime("%Y/%m/%d %H:%M:%S"), "IsDel": "0", "RoomID": "0"}
    value.update(patch)
    return value


def detail(row, student):
    return AppCheckIn.from_payload({
        "id": row["ID"], "tClassID": row["TClassID"], "checkInCode": row["CheckInCode"],
        "checkInType": row["CheckInType"], "createrDate": row["CreaterDate"],
        "validDate": row["ValidDate"], "isDel": row["IsDel"],
        "studentID": student["StudentID"],
    })


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.session = Mock(spec=requests.Session)
        self.api = DuifeneApi(self.session, self.settings, RateLimiter(self.settings))
        self.api._app_api._store_login_payload({"success": True, "code": 200, "data": {
            "token": "test-token", "refreshToken": "test-refresh", "userInfo": {
                "userId": "self", "userLogin": "student", "userTypeID": "2"}}}, initial_login=True)
        self.course = Course("course", "class", "课程")
        self.api._app_api.fetch_courses = Mock(return_value=[self.course])
        self.api.fetch_courses()
        self.api._student_web = Mock()
        self.api._student_web.fetch_numeric_materials.return_value = {}
        self.api._app_api.fetch_checkin = Mock(return_value=detail(class_row(), record()))
        self.session.request.return_value = response({"success": True, "data": [record()]})

    def test_student_discovery_preserves_material_when_detail_has_it(self):
        activities = self.api.fetch_active_activities(self.course)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].code, "0073")
        self.assertEqual(activities[0].student_id, "self")
        self.assertTrue(activities[0].has_valid_times)
        self.assertIn("/CK/GetStudentInLogList/class/self", self.session.request.call_args.args[1])
        self.assertNotIn("GetModelByInProgress", self.session.request.call_args.args[1])

    def test_multiple_activities_are_not_reduced_to_latest_only(self):
        self.session.request.return_value = response({"success": True, "data": [record(), record("102")]})
        self.api._app_api.fetch_checkin.side_effect = lambda activity_id: detail(class_row(activity_id), record(activity_id))
        self.assertEqual({a.activity_id for a in self.api.fetch_active_activities(self.course)}, {"101", "102"})

    def test_unknown_course_never_sends_queries(self):
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(Course("other", "class", "其他课程"))
        self.session.request.assert_not_called()
        self.api._app_api.fetch_checkin.assert_not_called()

    def test_foreign_student_record_never_queries_code(self):
        self.session.request.return_value = response({"success": True, "data": [record(StudentID="someone-else")]})
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(self.course)
        self.api._app_api.fetch_checkin.assert_not_called()

    def test_official_absence_placeholder_uses_authenticated_student(self):
        self.session.request.return_value = response({"success": True, "data": [record(StudentID=None, CheckInStatus=None, CheckInDate=None)]})
        activities = self.api.fetch_active_activities(self.course)
        self.assertEqual(activities[0].student_id, "self")
        self.assertEqual(activities[0].code, "0073")

    def test_null_student_on_nonabsence_record_is_rejected(self):
        self.session.request.return_value = response({"success": True, "data": [record(StudentID=None, StatusID="3")]})
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(self.course)
        self.api._app_api.fetch_checkin.assert_not_called()

    def test_signed_record_with_null_student_id_is_skipped(self):
        self.session.request.return_value = response({"success": True, "data": [record(
            StudentID=None, StatusID=1, CheckInStatus=1,
            CheckInDate=datetime.now(CHINA_TZ).isoformat(), StatusName="出勤",
        )]})
        self.assertEqual(self.api.fetch_active_activities(self.course), [])
        self.api._app_api.fetch_checkin.assert_not_called()

    def test_inconsistent_signed_record_with_null_id_is_rejected(self):
        self.session.request.return_value = response({"success": True, "data": [record(
            StudentID=None, StatusID=1, CheckInStatus=1, CheckInDate=None,
        )]})
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(self.course)
        self.api._app_api.fetch_checkin.assert_not_called()

    def test_ended_or_deleted_activity_is_not_returned(self):
        for row in (class_row(IsDel="1"), class_row(ValidDate=(datetime.now(CHINA_TZ)-timedelta(seconds=5)).strftime("%Y/%m/%d %H:%M:%S"))):
            with self.subTest(row=row):
                self.api._app_api.fetch_checkin.return_value = detail(row, record())
                self.assertEqual(self.api.fetch_active_activities(self.course), [])

    def test_bad_time_is_not_treated_as_active(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(ValidDate="invalid"), record())
        with self.assertRaises(AppApiError):
            self.api.fetch_active_activities(self.course)

    def test_fallback_detail_must_match_own_class(self):
        self.api._app_api.fetch_checkin = Mock(return_value=detail(class_row(TClassID="other"), record()))
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(self.course)

    def test_missing_code_is_still_discovered_without_inventing_material(self):
        # 实测：学生详情不含实时码，不能用旧记录的码代替。
        self.api._app_api.fetch_checkin = Mock(return_value=detail(class_row(CheckInCode=""), record()))
        activities = self.api.fetch_active_activities(self.course)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].activity_id, "101")
        self.assertEqual(activities[0].code, "")
        self.assertEqual(activities[0].student_id, "self")

    def test_student_numeric_material_is_bound_to_activity_and_keeps_zero(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInCode=""), record())
        self.api._student_web.fetch_numeric_materials.return_value = {"101": class_row(CheckInCode="0073")}
        activities = self.api.fetch_active_activities(self.course)
        self.assertEqual(activities[0].code, "0073")
        self.api._student_web.fetch_numeric_materials.assert_called_once_with("class", {"101"})

    def test_expired_numeric_material_is_not_returned(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInCode=""), record())
        self.api._student_web.fetch_numeric_materials.return_value = {"101": class_row(
            ValidDate=(datetime.now(CHINA_TZ)-timedelta(seconds=1)).isoformat())}
        self.assertEqual(self.api.fetch_active_activities(self.course), [])

    def test_numeric_material_with_changed_type_is_rejected(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInCode=""), record())
        self.api._student_web.fetch_numeric_materials.return_value = {"101": class_row(CheckInType="2")}
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(self.course)

    def test_deleted_numeric_material_is_not_returned(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInCode=""), record())
        self.api._student_web.fetch_numeric_materials.return_value = {"101": class_row(IsDel=1)}
        self.assertEqual(self.api.fetch_active_activities(self.course), [])

    def test_integer_numeric_material_is_not_guessed_or_padded(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInCode=""), record())
        self.api._student_web.fetch_numeric_materials.return_value = {"101": class_row(CheckInCode=73)}
        self.assertEqual(self.api.fetch_active_activities(self.course)[0].code, "")

    def test_numeric_refresh_does_not_repeat_app_record_or_detail_queries(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInCode=""), record())
        activities = self.api.fetch_active_activities(self.course, include_numeric_materials=False)
        self.api._student_web.fetch_numeric_materials.assert_not_called()
        self.session.request.reset_mock()
        self.api._app_api.fetch_checkin.reset_mock()
        self.api.refresh_numeric_materials(self.course, activities)
        self.api.refresh_numeric_materials(self.course, activities)
        self.assertEqual(self.api._student_web.fetch_numeric_materials.call_count, 2)
        self.session.request.assert_not_called()
        self.api._app_api.fetch_checkin.assert_not_called()

    def test_numeric_refresh_rejects_snapshot_from_other_student(self):
        snapshot = replace(detail(class_row(CheckInCode=""), record()), student_id="other")
        with self.assertRaises(PermissionDenied):
            self.api.refresh_numeric_materials(self.course, [snapshot])
        self.api._student_web.fetch_numeric_materials.assert_not_called()

    def test_teacher_identity_is_not_used_for_student_monitoring(self):
        self.api._app_api._identity = AppIdentity("teacher", "self", "", "1")
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(self.course)
        self.session.request.assert_not_called()

    def test_clear_removes_course_membership(self):
        self.api.clear_session()
        self.assertFalse(self.api.app_authenticated)
        self.assertEqual(self.api._known_courses, set())

    def test_only_qr_type_uses_student_web_material(self):
        self.api._student_web = Mock()
        self.api._student_web.fetch_token.return_value = "A" * 48
        self.api.fetch_active_activities(self.course)
        self.api._student_web.fetch_token.assert_not_called()
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInType="2"), record())
        activities = self.api.fetch_active_activities(self.course)
        self.assertEqual(activities[0].qr_token, "A" * 48)
        self.api._student_web.fetch_token.assert_called_once_with("course")

    def test_multiple_activities_never_share_course_qr_material(self):
        self.api._student_web = Mock()
        self.session.request.return_value = response({"success": True, "data": [record(), record("102")]})
        self.api._app_api.fetch_checkin.side_effect = lambda activity_id: detail(class_row(activity_id, CheckInType="2"), record(activity_id))
        self.assertEqual(len(self.api.fetch_active_activities(self.course)), 2)
        self.api._student_web.fetch_token.assert_not_called()

    def test_qr_activity_ended_after_image_read_is_not_returned(self):
        self.api._student_web = Mock()
        self.api._student_web.fetch_token.return_value = "A" * 48
        first = detail(class_row(CheckInType="2"), record())
        self.api._app_api.fetch_checkin.side_effect = [first, replace(first, valid_date=(datetime.now(CHINA_TZ)-timedelta(seconds=1)).isoformat())]
        self.assertEqual(self.api.fetch_active_activities(self.course), [])

    def test_multiple_joined_classes_do_not_use_course_wide_qr(self):
        self.api._known_courses.add(("course", "second-class"))
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInType="2"), record())
        self.api._student_web = Mock()
        self.api.fetch_active_activities(self.course)
        self.api._student_web.fetch_token.assert_not_called()

    def test_activity_type_changed_during_qr_read_is_rejected(self):
        first = detail(class_row(CheckInType="2"), record())
        self.api._app_api.fetch_checkin.side_effect = [first, replace(first, check_type="1")]
        self.api._student_web = Mock()
        self.api._student_web.fetch_token.return_value = "A" * 48
        with self.assertRaises(PermissionDenied):
            self.api.fetch_active_activities(self.course)

    def test_web_failure_preserves_app_session(self):
        self.api._app_api.fetch_checkin.return_value = detail(class_row(CheckInType="2"), record())
        self.api._student_web = Mock()
        self.api._student_web.fetch_token.side_effect = WebSessionError("学生网页登录失败")
        with self.assertRaises(WebSessionError):
            self.api.fetch_active_activities(self.course)
        self.assertTrue(self.api.app_authenticated)

    def test_different_saved_account_is_not_used_for_web_login(self):
        self.api._credentials = Mock()
        self.api._credentials.load.return_value = SavedCredentials("other", "test")
        self.assertIsNone(self.api._web_credentials())


class SubmissionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.identity = AppIdentity("student", "self", "", "2")
        self.api.limiter.is_blocking.return_value = False
        self.api.fetch_headcount.return_value = (1, 1)
        self.api.sign_by_code.return_value = "签到成功"
        self.logs = []
        self.processor = ActivityProcessor(Settings(), self.api, self.logs.append)
        self.activity = detail(class_row(), record())
        self.course = Course("course", "class", "课程")

    def test_valid_material_preserves_leading_zero_on_submission(self):
        self.assertEqual(self.processor.process(self.activity, self.course), Outcome.SIGNED)
        self.api.sign_by_code.assert_called_once_with("0073")

    def test_missing_or_invalid_code_never_queries_count_or_submits(self):
        for code in ("", "123", "１２３４", "abcd"):
            with self.subTest(code=code):
                self.assertEqual(self.processor.process(replace(self.activity, code=code), self.course), Outcome.DEFERRED)
        self.api.fetch_headcount.assert_not_called()
        self.api.fetch_checkin.assert_not_called()
        self.api.sign_by_code.assert_not_called()

    def test_unknown_time_never_queries_count_or_submits(self):
        self.assertEqual(self.processor.process(replace(self.activity, valid_date="bad"), self.course), Outcome.DEFERRED)
        self.api.fetch_headcount.assert_not_called()
        self.api.sign_by_code.assert_not_called()

    def test_identity_changed_after_discovery_cannot_submit(self):
        self.api.identity = AppIdentity("other", "other-student", "", "2")
        with self.assertRaises(AuthError):
            self.processor.process(self.activity, self.course)
        self.api.sign_by_code.assert_not_called()

    def test_watcher_processes_each_returned_activity(self):
        self.api.fetch_active_activities.return_value = [self.activity, replace(self.activity, activity_id="102")]
        processor = Mock()
        watcher = Watcher(Settings(), self.api, processor, self.logs.append)
        watcher.set_course(self.course)
        watcher._in_monitor_window = Mock(return_value=True)
        watcher._tick()
        self.assertEqual(processor.process.call_count, 2)
        self.api.fetch_active_activities.assert_called_once_with(self.course, include_numeric_materials=False)

    def test_discovery_interval_is_independent_of_numeric_deadline(self):
        settings = Settings(poll_min_s=4, poll_max_s=4)
        watcher = Watcher(settings, self.api, Mock(), self.logs.append)
        self.assertEqual(watcher._poll_delay(), 4)
        watcher._numeric_deadline = datetime.now(CHINA_TZ) + timedelta(seconds=60)
        self.assertEqual(watcher._poll_delay(), 4)
        watcher._numeric_deadline = datetime.now(CHINA_TZ) + timedelta(seconds=3)
        self.assertEqual(watcher._poll_delay(), 4)
        watcher._numeric_deadline = datetime.now(CHINA_TZ) - timedelta(seconds=1)
        self.assertEqual(watcher._poll_delay(), 4)

    def test_fast_numeric_poll_does_not_repeat_activity_discovery(self):
        missing = replace(self.activity, code="")
        self.api.fetch_active_activities.return_value = [missing]
        self.api.refresh_numeric_materials.return_value = [missing]
        processor = Mock()
        processor.process.return_value = Outcome.DEFERRED
        watcher = Watcher(Settings(), self.api, processor, self.logs.append)
        watcher.set_course(self.course)
        watcher._in_monitor_window = Mock(return_value=True)
        watcher._tick()
        watcher._numeric_tick()
        watcher._numeric_tick()
        self.assertEqual(self.api.fetch_active_activities.call_count, 1)
        self.assertEqual(self.api.refresh_numeric_materials.call_count, 2)

    def test_ready_numeric_code_submits_and_stops_fast_poll(self):
        self.api.fetch_active_activities.return_value = [replace(self.activity, code="")]
        self.api.refresh_numeric_materials.return_value = [self.activity]
        watcher = Watcher(Settings(), self.api, self.processor, self.logs.append)
        watcher.set_course(self.course)
        watcher._in_monitor_window = Mock(return_value=True)
        watcher._tick()
        watcher._numeric_tick()
        watcher._numeric_tick()
        self.api.sign_by_code.assert_called_once_with("0073")
        self.assertEqual(watcher._pending_numeric, {})
        self.assertEqual(self.api.fetch_active_activities.call_count, 1)

    def test_run_schedules_discovery_every_three_seconds_and_material_faster(self):
        watcher = Watcher(Settings(poll_min_s=3, poll_max_s=3), self.api, Mock(), self.logs.append)
        clock = [0.0]
        def discover():
            watcher._pending_numeric = {"101": self.activity}
        def sleep(seconds):
            clock[0] += max(seconds, 0.000001)
            if clock[0] >= 6.1:
                watcher._stop.set()
        watcher._tick = Mock(side_effect=discover)
        watcher._numeric_tick = Mock()
        watcher._sleep = sleep
        with patch("duifene.watcher.time.monotonic", side_effect=lambda: clock[0]):
            watcher._run()
        self.assertEqual(watcher._tick.call_count, 3)
        self.assertGreaterEqual(watcher._numeric_tick.call_count, 25)
        self.assertLessEqual(watcher._numeric_tick.call_count, 35)

    def test_invalid_numeric_code_also_enters_fast_material_poll(self):
        self.api.fetch_active_activities.return_value = [replace(self.activity, code="１２３４")]
        watcher = Watcher(Settings(), self.api, self.processor, self.logs.append)
        watcher.set_course(self.course)
        watcher._in_monitor_window = Mock(return_value=True)
        watcher._tick()
        self.assertIn(self.activity.activity_id, watcher._pending_numeric)
        self.api.sign_by_code.assert_not_called()

    def test_last_five_seconds_prioritize_material_over_due_discovery(self):
        watcher = Watcher(Settings(), self.api, Mock(), self.logs.append)
        watcher._set_pending_numeric([replace(
            self.activity, code="", valid_date=(datetime.now(CHINA_TZ) + timedelta(seconds=3)).isoformat(),
        )])
        watcher._tick = Mock()
        watcher._numeric_tick = Mock(side_effect=watcher._stop.set)
        watcher._run()
        watcher._tick.assert_not_called()
        watcher._numeric_tick.assert_called_once()

    def test_expired_activity_leaves_fast_material_poll_without_query(self):
        watcher = Watcher(Settings(), self.api, Mock(), self.logs.append)
        watcher.set_course(self.course)
        watcher._in_monitor_window = Mock(return_value=True)
        watcher._set_pending_numeric([replace(
            self.activity, code="", valid_date=(datetime.now(CHINA_TZ) - timedelta(seconds=1)).isoformat(),
        )])
        watcher._numeric_tick()
        self.assertEqual(watcher._pending_numeric, {})
        self.api.refresh_numeric_materials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
