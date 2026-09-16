"""领域模型：课程、签到活动、处理结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping

DATE_FMT = "%Y/%m/%d %H:%M:%S"


@dataclass(frozen=True)
class Course:
    """一门课程（含其教学班标识）。"""

    course_id: str
    class_id: str
    name: str


@dataclass(frozen=True)
class CheckInActivity:
    """一条签到活动记录。构造后不可变，便于在轮次间安全传递。"""

    id: str
    type: str
    code: str
    status_id: str
    status_name: str
    can_apply: str
    check_in_status: str
    check_in_date: str
    apply_limit_date: str
    create_date: str

    @classmethod
    def from_raw(cls, row: Mapping[str, Any]) -> "CheckInActivity":
        """从 MBCount.ashx `getstudentinlogbyday` 返回的单行构造。"""
        def get(key: str) -> str:
            return str(row.get(key) or "")

        return cls(
            id=get("ID"),
            type=get("CheckInType"),
            code=get("CheckInCode"),
            status_id=get("StatusID"),
            status_name=get("StatusName"),
            can_apply=get("CanApply"),
            check_in_status=get("CheckInStatus"),
            check_in_date=get("CheckInDate"),
            apply_limit_date=get("ApplyLimitDate"),
            create_date=get("CreaterDate"),
        )

    def remaining_seconds(self, offset_hours: int, now: datetime | None = None) -> int:
        """距签到结束还剩的秒数（负数表示已结束）。

        ``ApplyLimitDate`` 是补签截止时间，比签到结束晚 ``offset_hours`` 小时。
        """
        try:
            limit = datetime.strptime(self.apply_limit_date, DATE_FMT) - timedelta(
                hours=offset_hours
            )
        except ValueError:
            return 0
        return int((limit - (now or datetime.now())).total_seconds())

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """活动已创建多少秒；创建时间不可解析时返回 None。"""
        try:
            created = datetime.strptime(self.create_date, DATE_FMT)
        except ValueError:
            return None
        return ((now or datetime.now()) - created).total_seconds()


class Outcome(Enum):
    """单个活动的处理结果。"""

    SIGNED = "signed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"  # 条件未满足，保留待下一轮
