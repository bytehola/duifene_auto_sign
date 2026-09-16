"""教室坐标反推：距离预言机 + 三边测量（业务核心）。"""

from __future__ import annotations

import math

import requests

from .api import DuifeneApi
from .errors import RateLimited
from .models import Course
from .settings import Settings
from .trilateration import haversine, solve_trilateration


class LocationResolver:
    """以学生权限反推教室坐标。

    以中性城市为圆心、在其周围大半径环上多方位测距，再用三边测量解出教室
    坐标。使用中性城市而非学校真实位置，避免从探测点分布反推出使用者所在
    区域；采样中心只决定探测环圆心，不影响解算结果。

    本类不产生日志：反推过程（探测点、残差、耗时）对用户无价值，只把最终
    坐标与拟合残差返回给调用方，由调用方决定如何记录结果。
    """

    def __init__(self, settings: Settings, api: DuifeneApi) -> None:
        self._settings = settings
        self._api = api

    def resolve(self, course: Course) -> tuple[float, float, float] | None:
        """远场多方位测距后反推教室坐标，返回 `(经度, 纬度, 拟合残差米)`；
        采样不足或残差过大返回 None。"""
        center_lat, center_lon = self._settings.probe_center
        samples: list[tuple[float, float, float]] = []
        for index in range(self._settings.probe_azimuths):
            angle = 2 * math.pi * index / self._settings.probe_azimuths + 0.1
            dlat = (
                self._settings.probe_radius_m
                * math.sin(angle)
                / self._settings.meters_per_deg_lat
            )
            dlon = self._settings.probe_radius_m * math.cos(angle) / (
                self._settings.meters_per_deg_lon * math.cos(math.radians(center_lat))
            )
            lat, lon = center_lat + dlat, center_lon + dlon
            try:
                distance = self._api.probe_distance(course, lon, lat)
            except RateLimited:
                # 限流时立即中止：继续探测只会不断重置服务端计时、加剧封锁。
                break
            except requests.exceptions.RequestException:
                # 单点网络失败不应中断整个反推，跳过该点继续采样。
                continue
            if distance is not None:
                samples.append((lat, lon, distance))
        if len(samples) < self._settings.probe_min_samples:
            return None
        try:
            lat, lon = solve_trilateration(samples)
        except Exception:
            return None
        residual = max(
            abs(haversine(la, lo, lat, lon) - dist)
            for la, lo, dist in samples
        )
        if residual > self._settings.probe_max_residual_m:  # 残差过大说明数据异常
            return None
        return lon, lat, residual
