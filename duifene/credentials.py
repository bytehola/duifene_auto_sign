"""账号密码持久化。

Windows 使用 DPAPI 加密密码；其他平台使用权限受限的配置文件保存，
便于开发环境运行测试。
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

from .config import IniStore

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


@dataclass(frozen=True)
class SavedCredentials:
    """本地保存的登录账号与密码。"""

    account: str
    password: str


class _DataBlob(ctypes.Structure):
    """Win32 ``DATA_BLOB``。"""

    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    )


class CredentialStore:
    """读写账号密码，并在 Windows 上用当前用户密钥加密密码。"""

    def __init__(self, store: IniStore) -> None:
        self._store = store

    def load(self) -> SavedCredentials | None:
        """读取并解密账号密码；不存在或无法解密时返回 None。"""
        values = self._store.load_saved_credentials()
        account = str(values.get("account") or "")
        scheme = str(values.get("password_scheme") or "")
        password_data = str(values.get("password_data") or "")
        if not account or not scheme or not password_data:
            return None
        try:
            if scheme == "dpapi":
                password = _unprotect_windows(password_data)
            elif scheme == "plain":
                password = password_data
            else:
                return None
        except (OSError, ValueError):
            return None
        if not password:
            return None
        return SavedCredentials(account=account, password=password)

    def save(self, account: str, password: str) -> None:
        """保存账号密码。"""
        account = str(account or "").strip()
        password = str(password or "")
        if not account or not password:
            return
        if sys.platform == "win32":
            password_scheme = "dpapi"
            password_data = _protect_windows(password)
        else:
            password_scheme = "plain"
            password_data = password
        self._store.save_credentials(
            {
                "account": account,
                "password_scheme": password_scheme,
                "password_data": password_data,
            }
        )

    def clear(self) -> None:
        """删除保存的账号密码。"""
        self._store.clear_credentials()


def _protect_windows(value: str) -> str:
    """使用 Windows DPAPI 加密文本，返回 Base64。"""
    if sys.platform != "win32":
        raise OSError("DPAPI 仅在 Windows 上可用")
    raw = value.encode("utf-8")
    input_buffer = ctypes.create_string_buffer(raw, len(raw))
    input_blob = _DataBlob(
        len(raw),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char)),
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        ctypes.c_wchar_p("duifene"),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
    return base64.b64encode(encrypted).decode("ascii")


def _unprotect_windows(value: str) -> str:
    """解密 Windows DPAPI 生成的 Base64 文本。"""
    if sys.platform != "win32":
        raise OSError("DPAPI 仅在 Windows 上可用")
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    input_buffer = ctypes.create_string_buffer(raw, len(raw))
    input_blob = _DataBlob(
        len(raw),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char)),
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        decrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
    return decrypted.decode("utf-8")
