"""签到业务核心：单个活动的判定与执行。"""

from __future__ import annotations

import time

from .api import DuifeneApi
from .config import IniStore
from .errors import LogFn, RateLimited
from .location import LocationResolver
from .models import CheckInActivity, Course, Outcome
from .push import PushMonitor
from .settings import Settings


class ActivityProcessor:
    """判定单个活动的可签性并执行签到。

    跨轮次状态（已处理集合、人数提示缓存、WS 等待起点）内聚于本类，
    由 :meth:`reset` 在切换课程或重新开始监听时清空。
    """

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        push: PushMonitor,
        resolver: LocationResolver,
        store: IniStore,
        log: LogFn,
    ) -> None:
        self._settings = settings
        self._api = api
        self._push = push
        self._resolver = resolver
        self._store = store
        self._log = log
        self._handled: set[str] = set()
        self._headcount_log: dict[str, tuple[int, int]] = {}
        self._wait_start: dict[str, float] = {}
        # 已出过结果的活动：签到失败不进 ``_handled``，活动会每轮重试；此集合
        # 作为静音开关，令同活动的提交与结果文案只在首轮输出，避免逐轮刷屏。
        self._result_logged: set[str] = set()

    def reset(self) -> None:
        """清空跨轮次状态。"""
        self._handled.clear()
        self._headcount_log.clear()
        self._wait_start.clear()
        self._result_logged.clear()

    def _log_attempt(self, activity_id: str, course: Course, message: str) -> None:
        """输出签到尝试文案；该活动已出过结果时静默。"""
        if activity_id in self._result_logged:
            return
        self._log_course(course, message)

    def _log_result(self, activity_id: str, course: Course, message: str) -> None:
        """输出签到结果文案，并把该活动标记为已出结果（后续静默）。"""
        if activity_id in self._result_logged:
            return
        self._result_logged.add(activity_id)
        self._log_course(course, message)

    def _log_course(self, course: Course, message: str) -> None:
        """带课程前缀的日志——多门课共用同一面板，前缀使记录可归属到课程。"""
        self._log(f"课程: {course.name} {message}")

    def process(self, activity: CheckInActivity, course: Course) -> Outcome:
        """处理一条活动，返回处理结果。"""
        if activity.id in self._handled:
            return Outcome.SKIPPED
        # 服务端已记录（含重启前所签）或已出勤：标记后不再关注
        if (
            activity.check_in_status == "1"
            or activity.status_id == "1"
            or activity.status_name == "出勤"
        ):
            self._mark(course, activity.id, "已签到，无需重复签到")
            return Outcome.SKIPPED
        if activity.can_apply != "1":
            return Outcome.SKIPPED
        if activity.remaining_seconds(self._settings.sign_end_offset_hours) < 0:
            self._mark(course, activity.id, "已超过签到结束时间")
            return Outcome.SKIPPED
        age = activity.age_seconds()
        if age is None:
            return Outcome.SKIPPED
        if age < 0 and -age > self._settings.future_tolerance_s:
            self._mark(course, activity.id, f"创建时间异常（{activity.create_date}）")
            return Outcome.SKIPPED
        if age > self._settings.stale_sign_minutes * 60:
            self._mark(
                course,
                activity.id,
                f"创建已超过 {self._settings.stale_sign_minutes} 分钟，视为旧活动",
            )
            return Outcome.SKIPPED
        if self._api.limiter.is_blocking():
            left = int(self._api.limiter.remaining())
            self._log_course(course, f"限流中，{left} 秒后恢复")
            return Outcome.DEFERRED
        gate = self._headcount_gate(activity, age, course)
        if gate is not None:
            return gate
        return self._execute(activity, course)

    def _headcount_gate(
        self, activity: CheckInActivity, age: float, course: Course
    ) -> Outcome | None:
        """人数门禁：需继续等待返回 :attr:`Outcome.DEFERRED`，可签返回 None。"""
        counts = self._api.fetch_headcount(activity.id)
        if counts is None or not counts[1]:
            return Outcome.DEFERRED
        signed, total = counts
        if total < self._settings.small_class_max:
            if age < self._settings.small_class_delay_s:
                wait = int(self._settings.small_class_delay_s - age)
                self._log_course(
                    course, f"活动 {activity.id} 小班 {total} 人，{wait} 秒后签到"
                )
                return Outcome.DEFERRED
            return None
        ratio = signed / total
        if ratio <= self._settings.headcount_ratio:
            marker = (signed, total)
            if self._headcount_log.get(activity.id) != marker:  # 仅人数变化时提示
                self._headcount_log[activity.id] = marker
                self._log_course(
                    course,
                    f"活动 {activity.id} 已签 {signed}/{total}（{ratio:.0%}），"
                    f"未达 {self._settings.headcount_ratio:.0%} 门槛，继续等待",
                )
            return Outcome.DEFERRED
        return None

    def _execute(self, activity: CheckInActivity, course: Course) -> Outcome:
        """按签到类型执行；命中限流时降级为 :attr:`Outcome.DEFERRED`。"""
        try:
            if activity.type == "1":
                outcome = self._sign_code(activity, course)
            elif activity.type == "2":
                outcome = self._sign_qr(activity, course)
            elif activity.type == "3":
                outcome = self._sign_location(activity, course)
            else:
                self._log_course(
                    course, f"活动 {activity.id} 类型未知（{activity.type}），跳过"
                )
                return Outcome.SKIPPED
        except RateLimited as exc:
            self._log_course(course, f"活动 {activity.id} {exc!s}")
            return Outcome.DEFERRED
        if outcome is Outcome.SIGNED:
            self._handled.add(activity.id)
            if str(activity.id) == self._push.subscribed_id:
                self._push.stop()
            self._wait_start.pop(activity.id, None)
        return outcome

    def _sign_code(self, activity: CheckInActivity, course: Course) -> Outcome:
        """签到码签到。"""
        if not (activity.code and len(activity.code) == 4):
            self._log_result(
                activity.id, course, f"活动 {activity.id} 未取到有效签到码，跳过"
            )
            return Outcome.SKIPPED
        self._log_attempt(
            activity.id, course, f"活动 {activity.id} 提交签到码 {activity.code}"
        )
        return self._report(activity.id, self._api.sign_by_code(activity.code), course)

    def _sign_qr(self, activity: CheckInActivity, course: Course) -> Outcome:
        """二维码签到。"""
        self._log_attempt(activity.id, course, f"活动 {activity.id} 提交二维码签到")
        return self._report(activity.id, self._api.sign_by_qr(activity.id), course)

    def _sign_location(self, activity: CheckInActivity, course: Course) -> Outcome:
        """定位签到：坐标取自 WS 推送 → 缓存 → 反推。

        过程不写日志（推送等待、缓存命中、反推采样等对用户无价值且刷屏），
        只记最终结果——失败统一为「定位签到失败，未取得教室坐标」，成功给出
        经纬度与距离。
        """
        aid = activity.id
        if self._push.subscribed_id != str(aid):
            if self._push.subscribe(aid):
                self._wait_start[aid] = time.time()
            else:
                # 订阅失败：跳过等待，直接进入回退链
                self._wait_start[aid] = time.time() - self._settings.ws_wait_timeout_s

        point = self._push.take_follow_point()
        if point is not None:
            lon, lat, distance = point
            if self._is_signed(self._api.sign_by_location(lon, lat)):
                self._store.save_center(course.class_id, lon, lat)
                self._log_signed_location(aid, course, lon, lat, distance)
                return Outcome.SIGNED

        waited = time.time() - self._wait_start.get(aid, 0)
        if waited < self._settings.ws_wait_timeout_s:
            return Outcome.DEFERRED

        cached = self._store.load_center(course.class_id)
        if cached is not None:
            if self._is_signed(self._api.sign_by_location(*cached)):
                self._log_signed_location(aid, course, *cached)
                return Outcome.SIGNED

        resolved = self._resolver.resolve(course)
        if resolved is None:
            self._log_result(aid, course, "定位签到失败，未取得教室坐标")
            return Outcome.SKIPPED
        lon, lat, residual = resolved
        message = self._api.sign_by_location(lon, lat)
        if self._is_signed(message):
            self._store.save_center(course.class_id, lon, lat)
            self._log_signed_location(aid, course, lon, lat, residual)
            return Outcome.SIGNED
        # 已解出坐标却被服务端拒绝：该文案含服务端原因（如距离），保留。
        self._log_result(aid, course, f"定位签到失败：{message or '无响应'}")
        return Outcome.SKIPPED

    def _log_signed_location(
        self,
        activity_id: str,
        course: Course,
        lon: float,
        lat: float,
        distance: float | None = None,
    ) -> None:
        """记录定位签到的成功结果。

        ``distance`` 跟随推送时为推送点距教室距离，反推解算时为拟合残差；
        缓存坐标无此值，则省略。
        """
        text = f"定位签到成功：经度 {lon:.6f}，纬度 {lat:.6f}"
        if distance is not None:
            text += f"，距离 {distance:.1f} 米"
        self._log_attempt(activity_id, course, text)

    @staticmethod
    def _is_signed(message: str | None) -> bool:
        """服务端文案是否表示签到成功。"""
        return bool(message) and "签到成功" in message and "未成功" not in message

    def _report(self, activity_id: str, message: str | None, course: Course) -> Outcome:
        """输出签到结果并归一化：把五花八门的服务端文案统一成
        「签到成功 / 签到失败：原因」一行，借关键字着色（成功→绿、失败→红）。

        结果只记一次——失败不进 ``_handled`` 会每轮重试。
        """
        if self._is_signed(message):
            self._log_result(activity_id, course, "签到成功")
            return Outcome.SIGNED
        self._log_result(activity_id, course, f"签到失败：{message or '无响应'}")
        return Outcome.SKIPPED

    def _mark(self, course: Course, activity_id: str, reason: str) -> None:
        """记录活动为已处理并打印原因，避免每轮重复提示。"""
        self._handled.add(activity_id)
        self._log_course(course, f"活动 {activity_id} {reason}，跳过")
