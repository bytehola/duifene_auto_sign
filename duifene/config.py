"""配置持久化：``duifenyi.ini`` 的读写。"""

from __future__ import annotations

import configparser
import os
from typing import Any, Mapping

SETTINGS_SECTION = "SETTINGS"
APP_SESSION_SECTION = "APP_SESSION"
ACCOUNT_SECTION = "ACCOUNT"


class IniStore:
    """管理 ``duifenyi.ini`` 中的运行参数。"""

    def __init__(self, path: str) -> None:
        self._path = path

    def ensure_exists(self) -> bool:
        """文件不存在时创建空模板；返回是否新建了文件。"""
        if os.path.exists(self._path):
            return False
        parser = configparser.ConfigParser()
        parser[SETTINGS_SECTION] = {}
        with open(self._path, "w", encoding="utf-8") as handle:
            parser.write(handle)
        return True

    def clear_legacy_login(self) -> None:
        """删除旧版本保存的网页 Cookie 与教室缓存。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        changed = False
        if parser.has_section("INFO"):
            parser.remove_section("INFO")
            changed = True
        for section in list(parser.sections()):
            if section.startswith("LOC_"):
                parser.remove_section(section)
                changed = True
        if not changed:
            return
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError:
            pass

    def load_app_session(self) -> dict[str, str]:
        """读取本地保存的 App 登录会话。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        if not parser.has_section(APP_SESSION_SECTION):
            return {}
        return dict(parser.items(APP_SESSION_SECTION))

    def save_app_session(self, values: Mapping[str, Any]) -> None:
        """保存 App 登录会话，并尽量限制配置文件权限。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        parser[APP_SESSION_SECTION] = {
            str(key): str(value) for key, value in values.items()
        }
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def clear_app_session(self) -> None:
        """删除本地保存的 App 登录会话。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        if not parser.has_section(APP_SESSION_SECTION):
            return
        parser.remove_section(APP_SESSION_SECTION)
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def load_saved_credentials(self) -> dict[str, str]:
        """读取本地保存的账号凭据。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        if not parser.has_section(ACCOUNT_SECTION):
            return {}
        return dict(parser.items(ACCOUNT_SECTION))

    def save_credentials(self, values: Mapping[str, Any]) -> None:
        """保存账号凭据，并尽量限制配置文件权限。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        parser[ACCOUNT_SECTION] = {
            str(key): str(value) for key, value in values.items()
        }
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def clear_credentials(self) -> None:
        """删除本地保存的账号凭据。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        if not parser.has_section(ACCOUNT_SECTION):
            return
        parser.remove_section(ACCOUNT_SECTION)
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def load_settings(self) -> dict[str, str]:
        """读取 ``[SETTINGS]`` 段原始键值。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        if not parser.has_section(SETTINGS_SECTION):
            return {}
        return dict(parser.items(SETTINGS_SECTION))

    def save_settings(self, values: Mapping[str, Any]) -> None:
        """写入 ``[SETTINGS]`` 段，保留其余段。"""
        parser = configparser.ConfigParser()
        parser.read(self._path, encoding="utf-8")
        parser[SETTINGS_SECTION] = {str(key): str(value) for key, value in values.items()}
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError:
            pass
