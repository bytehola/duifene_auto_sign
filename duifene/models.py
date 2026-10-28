"""领域模型：课程与处理结果。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Course:
    """课程及其当前教学班。"""

    course_id: str
    class_id: str
    name: str


class Outcome(Enum):
    """单个签到的处理结果。"""

    SIGNED = "signed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
