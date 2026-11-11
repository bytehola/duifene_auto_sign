"""签到业务核心：在 App 接口数据上执行四类签到。"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime

from .api import DuifeneApi
from .app_api import AppCheckIn, CHINA_TZ, parse_checkin_time
from .errors import AuthError, LogFn, RateLimited
from .models import Course, Outcome
from .settings import Settings

LOCATION_OFFSET_MIN_M = 5.0
LOCATION_OFFSET_MAX_M = 20.0
METERS_PER_DEGREE_LATITUDE = 111_132.92
METERS_PER_DEGREE_LONGITUDE = 111_320.0
HEADCOUNT_CACHE_TTL_S = 1.0
HEADCOUNT_CACHE_BYPASS_WINDOW_S = 5.0


class ActivityProcessor:
    """判定单个 App 签到是否可提交，并完成签到。"""

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        log: LogFn,
    ) -> None:
        self._settings = settings
        self._api = api
        self._log = log
        self._handled: set[str] = set()
        self._headcount_log: dict[str, tuple[int, int]] = {}
        self._headcount_cache: dict[tuple[str, str, str], tuple[float, tuple[int, int]]] = {}
        self._result_logged: set[str] = set()
        self._wait_logged: set[tuple[str, str]] = set()

    def reset(self) -> None:
        """清空跨轮次状态。"""
        self._handled.clear()
        self._headcount_log.clear()
        self._headcount_cache.clear()
        self._result_logged.clear()
        self._wait_logged.clear()

    def process(self, activity: AppCheckIn, course: Course) -> Outcome:
        """处理一次当前签到。"""
        activity_id = activity.activity_id
        if activity_id in self._handled:
            return Outcome.SKIPPED
        identity = self._api.identity
        if identity is None or activity.student_id != identity.user_id or activity.class_id != course.class_id:
            raise AuthError("签到活动与当前学生或教学班不匹配，请重新登录学生账号")
        if activity.is_deleted:
            self._mark(course, activity_id, "活动已作废")
            return Outcome.SKIPPED
        if not activity.has_valid_times:
            self._log_wait(activity_id, "活动有效期未知，暂不提交", course)
            return Outcome.DEFERRED
        if activity.is_attended:
            self._mark(course, activity_id, "已签到，无需重复签到")
            return Outcome.SKIPPED
        if activity.has_ended():
            self._mark(course, activity_id, "已超过签到结束时间")
            return Outcome.SKIPPED

        age = activity.age_seconds()
        if age is None or age < 0:
            self._log_wait(activity_id, "活动创建时间异常，暂不提交", course)
            return Outcome.DEFERRED
        if age > self._settings.stale_sign_minutes * 60:
            self._mark(
                course,
                activity_id,
                f"创建已超过 {self._settings.stale_sign_minutes} 分钟，视为旧活动",
            )
            return Outcome.SKIPPED
        if self._api.limiter.is_blocking():
            left = int(self._api.limiter.remaining())
            self._log_course(course, f"限流中，{left} 秒后恢复")
            return Outcome.DEFERRED

        if activity.check_type == "1" and not (
            len(activity.code) == 4 and activity.code.isascii() and activity.code.isdigit()
        ):
            self._log_wait(activity_id, "已发现活动，但当前学生查询未返回有效签到码", course)
            return Outcome.DEFERRED
        if activity.check_type == "4" and len(activity.code) != 4:
            self._log_wait(activity_id, "已发现活动，但当前学生查询未返回汉字答案", course)
            return Outcome.DEFERRED
        if activity.check_type == "2" and not activity.qr_token:
            self._log_wait(activity_id, "已发现活动，但当前学生查询未返回二维码令牌", course)
            return Outcome.DEFERRED

        gate = self._headcount_gate(activity, age, course)
        if gate is not None:
            return gate
        return self._execute(activity, course)

    def _headcount_gate(
        self,
        activity: AppCheckIn,
        age: float,
        course: Course,
    ) -> Outcome | None:
        """人数门禁：需等待时返回 DEFERRED，可提交时返回 None。"""
        cache_key = (activity.student_id, activity.class_id, activity.activity_id)
        cached = self._headcount_cache.get(cache_key)
        remaining = (parse_checkin_time(activity.valid_date) - datetime.now(CHINA_TZ)).total_seconds()
        # 截止窗口内人数可能刚刚更新，旧值不能占用仅剩的提交机会。
        if (remaining > HEADCOUNT_CACHE_BYPASS_WINDOW_S and cached is not None
                and time.monotonic() - cached[0] < HEADCOUNT_CACHE_TTL_S):
            counts = cached[1]
        else:
            counts = self._api.fetch_headcount(activity.activity_id)
            self._headcount_cache[cache_key] = (time.monotonic(), counts)
        signed, total = counts
        if total <= 0:
            return Outcome.DEFERRED
        if total < self._settings.small_class_max:
            if age < self._settings.small_class_delay_s:
                wait = int(self._settings.small_class_delay_s - age)
                self._log_course(
                    course,
                    f"活动 {activity.activity_id} 小班 {total} 人，{wait} 秒后签到",
                )
                return Outcome.DEFERRED
            return None

        ratio = signed / total
        if ratio <= self._settings.headcount_ratio:
            marker = (signed, total)
            if self._headcount_log.get(activity.activity_id) != marker:
                self._headcount_log[activity.activity_id] = marker
                self._log_course(
                    course,
                    f"活动 {activity.activity_id} 已签 {signed}/{total}"
                    f"（{ratio:.0%}），未达 {self._settings.headcount_ratio:.0%} "
                    f"门槛，继续等待",
                )
            return Outcome.DEFERRED
        return None

    def _execute(self, activity: AppCheckIn, course: Course) -> Outcome:
        """按 App 签到类型执行提交。"""
        try:
            if activity.check_type == "1":
                outcome = self._sign_code(activity, course)
            elif activity.check_type == "2":
                outcome = self._sign_qr(activity, course)
            elif activity.check_type == "3":
                outcome = self._sign_location(activity, course)
            elif activity.check_type == "4":
                outcome = self._sign_hanzi(activity, course)
            else:
                self._log_result(
                    activity.activity_id,
                    course,
                    f"活动 {activity.activity_id} 类型未知"
                    f"（{activity.check_type}），跳过",
                )
                return Outcome.SKIPPED
        except RateLimited as exc:
            self._log_course(course, f"活动 {activity.activity_id} {exc!s}")
            return Outcome.DEFERRED
        if outcome is Outcome.SIGNED:
            self._handled.add(activity.activity_id)
        return outcome

    def _sign_code(self, activity: AppCheckIn, course: Course) -> Outcome:
        """提交签到码。"""
        code = activity.code
        if len(code) != 4:
            self._log_wait(
                activity.activity_id,
                "未返回有效签到码",
                course,
            )
            return Outcome.DEFERRED
        self._log_attempt(
            activity.activity_id,
            course,
            f"活动 {activity.activity_id} 提交签到码 {code}",
        )
        return self._report(
            activity.activity_id,
            self._api.sign_by_code(code),
            course,
        )

    def _sign_qr(self, activity: AppCheckIn, course: Course) -> Outcome:
        """提交二维码令牌。"""
        token = activity.qr_token
        if not token:
            detail = self._api.fetch_checkin(activity.activity_id)
            token = detail.qr_token if detail is not None else ""
        if not token:
            self._log_wait(
                activity.activity_id,
                "未返回二维码令牌",
                course,
            )
            return Outcome.DEFERRED
        self._log_attempt(
            activity.activity_id,
            course,
            f"活动 {activity.activity_id} 提交二维码签到",
        )
        return self._report(
            activity.activity_id,
            self._api.sign_by_qr(token),
            course,
        )

    def _sign_hanzi(self, activity: AppCheckIn, course: Course) -> Outcome:
        """提交汉字签到答案。"""
        answer = activity.code
        if len(answer) != 4:
            detail = self._api.fetch_checkin(activity.activity_id)
            answer = detail.code if detail is not None else ""
        if len(answer) != 4:
            self._log_wait(
                activity.activity_id,
                "未返回有效汉字答案",
                course,
            )
            return Outcome.DEFERRED
        self._log_attempt(
            activity.activity_id,
            course,
            f"活动 {activity.activity_id} 提交汉字 {answer}",
        )
        return self._report(
            activity.activity_id,
            self._api.sign_by_code(answer),
            course,
        )

    def _sign_location(self, activity: AppCheckIn, course: Course) -> Outcome:
        """读取 App 教室坐标并提交定位签到。"""
        coordinates = self._api.fetch_room_coordinates(activity)
        if coordinates is None:
            self._log_wait(
                activity.activity_id,
                "未返回教室坐标",
                course,
            )
            return Outcome.DEFERRED
        longitude, latitude, offset_m = self._offset_coordinates(*coordinates)
        message = self._api.sign_by_location(longitude, latitude)
        if self._is_signed(message):
            self._log_result(
                activity.activity_id,
                course,
                f"定位签到成功：经度 {longitude:.6f}，纬度 {latitude:.6f}，"
                f"偏移 {offset_m:.1f} 米",
            )
            return Outcome.SIGNED
        self._log_result(
            activity.activity_id,
            course,
            f"定位签到失败：{message or '无响应'}",
        )
        return Outcome.SKIPPED

    @staticmethod
    def _offset_coordinates(
        longitude: float,
        latitude: float,
    ) -> tuple[float, float, float]:
        """在教室坐标周围按随机方向生成 5 到 20 米的签到点。"""
        distance_m = random.uniform(
            LOCATION_OFFSET_MIN_M,
            LOCATION_OFFSET_MAX_M,
        )
        direction_rad = random.uniform(0.0, 2 * math.pi)
        north_m = distance_m * math.sin(direction_rad)
        east_m = distance_m * math.cos(direction_rad)
        longitude_scale = METERS_PER_DEGREE_LONGITUDE * math.cos(
            math.radians(latitude)
        )
        longitude_offset = east_m / max(abs(longitude_scale), 1.0)
        latitude_offset = north_m / METERS_PER_DEGREE_LATITUDE
        return (
            longitude + longitude_offset,
            latitude + latitude_offset,
            distance_m,
        )

    def _log_attempt(self, activity_id: str, course: Course, message: str) -> None:
        """输出签到尝试文案。"""
        if activity_id in self._result_logged:
            return
        self._log_course(course, message)

    def _log_wait(self, activity_id: str, reason: str, course: Course) -> None:
        """同一等待原因只记录一次，后续轮次继续重试。"""
        marker = (activity_id, reason)
        if marker in self._wait_logged:
            return
        self._wait_logged.add(marker)
        self._log_course(course, f"活动 {activity_id} {reason}，继续重试")

    def _log_result(self, activity_id: str, course: Course, message: str) -> None:
        """输出最终结果文案，同一活动只显示一次。"""
        if activity_id in self._result_logged:
            return
        self._result_logged.add(activity_id)
        self._log_course(course, message)

    def _log_course(self, course: Course, message: str) -> None:
        """给日志加课程前缀。"""
        self._log(f"课程: {course.name} {message}")

    @staticmethod
    def _is_signed(message: str | None) -> bool:
        """服务端文案是否表示签到成功。"""
        return bool(message) and "签到成功" in message and "未成功" not in message

    def _report(
        self,
        activity_id: str,
        message: str | None,
        course: Course,
    ) -> Outcome:
        """归一化签到结果并返回处理状态。"""
        if self._is_signed(message):
            self._log_result(activity_id, course, "签到成功")
            return Outcome.SIGNED
        self._log_result(activity_id, course, f"签到失败：{message or '无响应'}")
        return Outcome.SKIPPED

    def _mark(self, course: Course, activity_id: str, reason: str) -> None:
        """记录已处理活动并输出原因。"""
        self._handled.add(activity_id)
        self._log_course(course, f"活动 {activity_id} {reason}，跳过")
