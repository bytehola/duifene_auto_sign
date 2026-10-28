"""运行参数。所有可调常量集中于此，避免魔数散落在业务逻辑中。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

# 可由用户在界面配置并持久化到 ini 的字段子集。
PERSISTABLE_FIELDS: tuple[str, ...] = (
    "poll_min_s",
    "poll_max_s",
    "error_backoff_max_s",
    "monitor_start_hour",
    "monitor_end_hour",
    "small_class_max",
    "small_class_delay_s",
    "headcount_ratio",
    "stale_sign_minutes",
    "cooldown_seconds",
    "proxy_url",
)

# 派生字段：随其它字段联动计算，不接受用户直接设置，但同样持久化。
DERIVED_FIELDS: tuple[str, ...] = ("verify_tls",)

# 各可持久化字段的取值闭区间 (下限, 上限)。
SETTING_LIMITS: dict[str, tuple[float, float]] = {
    "poll_min_s": (0.5, 60.0),
    "poll_max_s": (0.5, 300.0),
    "error_backoff_max_s": (5.0, 3600.0),
    "monitor_start_hour": (0, 24),
    "monitor_end_hour": (0, 24),
    "small_class_max": (1, 1000),
    "small_class_delay_s": (0.0, 600.0),
    "headcount_ratio": (0.0, 1.0),
    "stale_sign_minutes": (1, 1440),
    "cooldown_seconds": (0.0, 3600.0),
}


def _coerce(current: Any, raw: Any) -> tuple[bool, Any]:
    """按字段当前值的类型把外部输入转为合适类型。"""
    if isinstance(current, bool):
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True, True
        if text in ("0", "false", "no", "off"):
            return True, False
        return False, None
    if isinstance(current, int):
        try:
            return True, int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return False, None
    if isinstance(current, float):
        try:
            return True, float(str(raw).strip())
        except (TypeError, ValueError):
            return False, None
    return True, str(raw)


def _check_consistency(values: Mapping[str, Any]) -> str:
    """跨字段一致性校验；返回错误文案，通过时返回空串。"""
    if values["poll_min_s"] > values["poll_max_s"]:
        return "轮询最小间隔不能大于最大间隔"
    if values["monitor_start_hour"] >= values["monitor_end_hour"]:
        return "监听时间窗的起始小时须小于结束小时"
    proxy = _normalize_proxy(str(values.get("proxy_url", "")).strip())
    if proxy and not urlparse(proxy).hostname:
        return "代理地址格式有误，请填写形如 http://127.0.0.1:8080"
    return ""


def _normalize(values: dict[str, Any]) -> None:
    """归一化代理地址，并根据代理配置推导 TLS 校验开关。"""
    values["proxy_url"] = _normalize_proxy(str(values.get("proxy_url", "")).strip())
    values["verify_tls"] = not values["proxy_url"]


def _normalize_proxy(raw: str) -> str:
    """把代理地址规整为 requests 可用的 URL。"""
    text = raw.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


@dataclass
class Settings:
    """对分易 App 签到工具的运行时配置。"""

    app_host: str = "https://dfapp.duifene.com"
    user_agent: str = "okhttp/4.12.0"
    ini_filename: str = "duifenyi.ini"
    proxy_url: str = ""
    verify_tls: bool = True
    request_timeout_s: float = 10.0

    poll_min_s: float = 3.0
    poll_max_s: float = 5.0
    error_backoff_max_s: float = 60.0

    stale_sign_minutes: int = 30
    small_class_max: int = 5
    small_class_delay_s: float = 5.0
    headcount_ratio: float = 0.20

    monitor_start_hour: int = 6
    monitor_end_hour: int = 23

    cooldown_seconds: float = 300.0
    rate_limit_markers: tuple[str, ...] = ("过于频繁", "频繁")

    @property
    def proxies(self) -> dict[str, str] | None:
        """requests 代理映射；未配置时返回 None。"""
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def persistable_values(self) -> dict[str, Any]:
        """导出待持久化的字段值。"""
        names = PERSISTABLE_FIELDS + DERIVED_FIELDS
        return {name: getattr(self, name) for name in names}

    def editable_values(self) -> dict[str, Any]:
        """导出用户可直接编辑的字段值。"""
        return {name: getattr(self, name) for name in PERSISTABLE_FIELDS}

    def apply(self, patch: Mapping[str, Any]) -> str:
        """校验并应用一批配置更新（原地修改）。"""
        staged: dict[str, Any] = self.persistable_values()
        for name, raw in patch.items():
            if name in DERIVED_FIELDS:
                return f"字段 {name} 由系统派生，不可直接设置"
            if name not in PERSISTABLE_FIELDS:
                return f"不支持修改字段：{name}"
            ok, value = _coerce(getattr(self, name), raw)
            if not ok:
                return f"字段 {name} 取值非法：{raw!r}"
            limits = SETTING_LIMITS.get(name)
            if limits is not None:
                low, high = limits
                if not (low <= value <= high):
                    return f"字段 {name} 须在 {low} ~ {high} 之间"
            staged[name] = value
        error = _check_consistency(staged)
        if error:
            return error
        _normalize(staged)
        for name, value in staged.items():
            setattr(self, name, value)
        return ""
