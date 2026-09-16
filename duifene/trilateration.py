"""三边测量数学工具：Haversine 距离与教室坐标解算（无网络依赖）。

对分易定位签到把「与教室的距离」以纯文本返回给任何已登录学生：
    POST /_CheckIn/CheckInRoomHandler.ashx
    action=signin&cid=<课程>&tcid=<班>&sid=<学生>&latitude=<l>&longitude=<o>
  -> {"msg":-1,"msgbox":"不在教室范围，距离：<d>米！"}

距离 d 与两点坐标严格满足 Haversine（R=6378137，与前端 Distance() 一致），
精度到厘米级。因此在教室范围外取多个探测点，即可用最小二乘反推出
教室经纬度，而无需教师权限。探测与采样由 :mod:`duifene.location` 负责，
本模块只做纯计算。
"""

from __future__ import annotations

import math

R_EARTH = 6378137.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点间大圆距离（米），与服务端算法保持一致。"""
    r1, r2 = math.radians(lat1), math.radians(lat2)
    a = r1 - r2
    b = math.radians(lon1) - math.radians(lon2)
    return R_EARTH * 2 * math.asin(
        math.sqrt(math.sin(a / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(b / 2) ** 2)
    )


def solve_trilateration(
    points: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """解教室坐标：先用「圆相交」线性最小二乘给出初值，再用真实 Haversine 爬山细化。

    不需要任何先验位置。points 为 (lat, lon, distance_m) 列表，至少 3 个、
    且探测点方位分散（近共线会导致几何退化）。
    """
    if len(points) < 3:
        raise ValueError("至少需要 3 个探测点")

    # --- 1) 线性化：以第一个探测点为原点建立局部米制平面 ---
    lat0, lon0, _ = points[0]
    m_per_deg_lat = 111132.92
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    planar = [
        ((lo - lon0) * m_per_deg_lon, (la - lat0) * m_per_deg_lat, d)
        for la, lo, d in points
    ]
    x0, y0, d0 = planar[0]
    A = []
    b = []
    for x, y, d in planar[1:]:
        A.append([2.0 * (x - x0), 2.0 * (y - y0)])
        b.append((x * x - x0 * x0) + (y * y - y0 * y0) - (d * d - d0 * d0))
    sxx = sum(r[0] * r[0] for r in A)
    sxy = sum(r[0] * r[1] for r in A)
    syy = sum(r[1] * r[1] for r in A)
    sxb = sum(A[i][0] * b[i] for i in range(len(A)))
    syb = sum(A[i][1] * b[i] for i in range(len(A)))
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9 * (sxx + syy) ** 2:
        raise ValueError("探测点几何退化（近共线），无法定位，请增加方位分散的探测点")
    x = (syy * sxb - sxy * syb) / det
    y = (sxx * syb - sxy * sxb) / det
    lat = lat0 + y / m_per_deg_lat
    lon = lon0 + x / m_per_deg_lon

    # --- 2) 用真实 Haversine 做局部爬山细化 ---
    def cost(la: float, lo: float) -> float:
        return sum((haversine(pla, plo, la, lo) - d) ** 2 for pla, plo, d in points)

    cur = cost(lat, lon)
    step = 0.001
    while step > 1e-9:
        improved = False
        for dlat, dlon in (
            (step, 0), (-step, 0), (0, step), (0, -step),
            (step, step), (-step, -step), (step, -step), (-step, step),
        ):
            c = cost(lat + dlat, lon + dlon)
            if c < cur:
                cur, lat, lon = c, lat + dlat, lon + dlon
                improved = True
        if not improved:
            step /= 2
    return lat, lon
