"""运行参数。所有可调常量集中于此，避免魔数散落在业务逻辑中。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

# 可由用户在界面配置并持久化到 ini 的字段子集（其余为内部常量）。
PERSISTABLE_FIELDS: tuple[str, ...] = (
    "poll_min_s",
    "poll_max_s",
    "login_check_interval_s",
    "error_backoff_max_s",
    "monitor_start_hour",
    "monitor_end_hour",
    "small_class_max",
    "small_class_delay_s",
    "headcount_ratio",
    "stale_sign_minutes",
    "ws_follow_distance_m",
    "ws_wait_timeout_s",
    "cooldown_seconds",
    "proxy_url",
)

# 派生字段：随其它字段联动计算，不接受用户直接设置，但同样持久化。
DERIVED_FIELDS: tuple[str, ...] = ("verify_tls",)

# 各可持久化字段的取值闭区间 (下限, 上限)；未列出的字段不做范围校验。
SETTING_LIMITS: dict[str, tuple[float, float]] = {
    "poll_min_s": (0.5, 60.0),
    "poll_max_s": (0.5, 300.0),
    "login_check_interval_s": (5.0, 3600.0),
    "error_backoff_max_s": (5.0, 3600.0),
    "monitor_start_hour": (0, 24),
    "monitor_end_hour": (0, 24),
    "small_class_max": (1, 1000),
    "small_class_delay_s": (0.0, 600.0),
    "headcount_ratio": (0.0, 1.0),
    "stale_sign_minutes": (1, 1440),
    "ws_follow_distance_m": (0.0, 10000.0),
    "ws_wait_timeout_s": (0.0, 600.0),
    "cooldown_seconds": (0.0, 3600.0),
}


def _coerce(current: Any, raw: Any) -> tuple[bool, Any]:
    """按字段当前值的类型把外部输入转为合适类型，返回 `(是否成功, 值)`。"""
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
    """联动归一化（就地修改）。

    证书校验已不在前端暴露，其取值完全由代理决定：配置代理即视为抓包调试
    场景，其证书通常无法通过系统信任链校验，故关闭校验；无代理时恢复为安全
    的默认开启，避免清空代理后残留 False 且无从恢复。

    代理地址统一补全 scheme：requests 要求形如 ``http://host:port``，缺 scheme
    时会在发请求时抛 ProxySchemeUnknown，表现为「保存成功但代理无效」。用户常
    只填 ``127.0.0.1:8080``，这里补成 ``http://127.0.0.1:8080``。
    """
    values["proxy_url"] = _normalize_proxy(str(values.get("proxy_url", "")).strip())
    values["verify_tls"] = not values["proxy_url"]


def _normalize_proxy(raw: str) -> str:
    """把代理地址规整为 requests 可用的 URL；空串表示直连。"""
    text = raw.strip()
    if not text:
        return ""
    if "://" not in text:
        # 缺 scheme：按抓包工具常见约定补 http://
        text = "http://" + text
    return text


@dataclass
class Settings:
    """对分易签到工具的运行时配置。

    非 frozen：界面修改后可即时对已注入该实例的各模块生效（标量读写在
    CPython 下天然原子，足以满足单写多读场景）。
    """

    host: str = "https://www.duifene.com"
    hub_url: str = "https://wsdf.duifene.com/messageHub"
    user_agent: str = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
        "MicroMessenger/8.0.40(0x1800282a) NetType/WIFI Language/zh_CN "
    )
    ini_filename: str = "duifenyi.ini"
    # 正式运行不启用明文代理、开启 TLS 校验。抓包调试时在设置中显式填写
    # 代理地址并关闭校验。
    proxy_url: str = ""
    verify_tls: bool = True
    request_timeout_s: float = 10.0
    # 轮询：区间内随机，避免固定节律被风控识别
    poll_min_s: float = 3.0
    poll_max_s: float = 5.0
    login_check_interval_s: float = 30.0
    # 连续异常时的退避上限（秒）：异常越多等待越久，避免异常风暴下空转刷屏
    error_backoff_max_s: float = 60.0
    # 活动筛选
    sign_end_offset_hours: int = 24
    stale_sign_minutes: int = 30
    future_tolerance_s: float = 300.0
    # 人数门禁
    small_class_max: int = 5
    small_class_delay_s: float = 5.0
    headcount_ratio: float = 0.20
    # 监听时间窗（左闭右开）
    monitor_start_hour: int = 6
    monitor_end_hour: int = 23
    # 限流
    cooldown_seconds: float = 300.0
    rate_limit_markers: tuple[str, ...] = ("过于频繁", "频繁")
    # WebSocket 跟随
    ws_follow_distance_m: float = 50.0
    ws_wait_timeout_s: float = 15.0
    # 定位
    location_jitter_deg: float = 0.000089
    probe_center: tuple[float, float] = (39.9042, 116.4074)
    probe_radius_m: float = 800_000.0
    probe_azimuths: int = 6
    probe_min_samples: int = 3
    probe_max_residual_m: float = 50.0
    meters_per_deg_lat: float = 111132.92
    meters_per_deg_lon: float = 111320.0

    @property
    def proxies(self) -> dict[str, str] | None:
        """requests 代理映射；未配置时返回 None。"""
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def persistable_values(self) -> dict[str, Any]:
        """导出待持久化的字段值（:data:`PERSISTABLE_FIELDS` + 派生字段）。"""
        names = PERSISTABLE_FIELDS + DERIVED_FIELDS
        return {name: getattr(self, name) for name in names}

    def editable_values(self) -> dict[str, Any]:
        """导出用户可直接编辑的字段值（不含派生字段），供前端渲染表单。"""
        return {name: getattr(self, name) for name in PERSISTABLE_FIELDS}

    def apply(self, patch: Mapping[str, Any]) -> str:
        """校验并应用一批配置更新（原地修改）。

        仅接受 :data:`PERSISTABLE_FIELDS` 中的字段；逐字段做类型转换与范围
        校验，再整体做跨字段一致性校验与派生字段归一化，全部通过后才写入，
        保证原子性。派生字段（如 :data:`DERIVED_FIELDS`）不允许直接设置。
        """
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
