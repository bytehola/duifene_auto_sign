"""配置持久化：``duifenyi.ini`` 的读写。"""

from __future__ import annotations

import configparser
import os
from typing import Any, Mapping

SETTINGS_SECTION = "SETTINGS"


class IniStore:
    """管理 `duifenyi.ini`：登录 Cookie、运行参数与按班级缓存的教室坐标。"""

    def __init__(self, path: str) -> None:
        self._path = path

    def ensure_exists(self) -> bool:
        """文件不存在时创建空模板；返回是否新建了文件。"""
        if os.path.exists(self._path):
            return False
        parser = configparser.ConfigParser()
        parser["INFO"] = {"cookie": ""}
        with open(self._path, "w", encoding="utf-8") as handle:
            parser.write(handle)
        return True

    def load_cookie(self) -> str | None:
        """读取保存的 Cookie 字符串；无则返回 None。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        return parser.get("INFO", "cookie", fallback=None)

    def save_cookie(self, cookie: str) -> None:
        """写回 Cookie。

        先读后写以保留 `[LOC_*]` 等其它段；写盘失败静默忽略，不中断签到主流程。
        """
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        parser["INFO"] = {"cookie": cookie}
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError:
            pass

    def load_settings(self) -> dict[str, str]:
        """读取 `[SETTINGS]` 段原始键值（未做类型转换，由 Settings 校验）。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        if not parser.has_section(SETTINGS_SECTION):
            return {}
        return dict(parser.items(SETTINGS_SECTION))

    def save_settings(self, values: Mapping[str, Any]) -> None:
        """写入 `[SETTINGS]` 段，保留其余段。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        parser[SETTINGS_SECTION] = {str(k): str(v) for k, v in values.items()}
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError:
            pass

    def load_center(self, class_id: str) -> tuple[float, float] | None:
        """读取该班级缓存的教室坐标 `(经度, 纬度)`；无缓存或解析失败返回 None。

        以教学班 ID 为键：每门课通常对应固定教室。
        """
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        lon = parser.get(f"LOC_{class_id}", "longitude", fallback="")
        lat = parser.get(f"LOC_{class_id}", "latitude", fallback="")
        if not (lon and lat):
            return None
        try:
            return float(lon), float(lat)
        except ValueError:
            return None

    def save_center(self, class_id: str, longitude: float, latitude: float) -> None:
        """缓存该班级教室坐标供下次复用；写盘失败静默忽略。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        parser[f"LOC_{class_id}"] = {
            "longitude": f"{longitude:.8f}",
            "latitude": f"{latitude:.8f}",
        }
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError:
            pass
