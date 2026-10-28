"""程序入口：在 pywebview 中启动对分易自动签到。"""

from __future__ import annotations

import os
import sys

from duifene import __version__
from duifene.bridge import Bridge
from duifene.settings import Settings

# 窗口与加载覆盖层共用的底色，保证揭幕时无跳色。
_BACKGROUND = "#f7f9f9"
_PYTHONNET_RUNTIME_RELATIVE_PATH = (
    "_internal",
    "pythonnet",
    "runtime",
    "Python.Runtime.dll",
)


def resource_path(*parts: str) -> str:
    """解析随程序分发的静态资源路径，返回绝对路径。

    PyInstaller 冻结后资源被解包到 ``sys._MEIPASS``；源码运行时相对脚本目录。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def _unblock_pythonnet_runtime(root: str | os.PathLike[str] | None = None) -> bool:
    """移除 Python.Runtime.dll 的 Windows 网络来源标记。

    .NET Framework 会拒绝加载带 ``Zone.Identifier`` 的托管程序集。
    仅处理 pythonnet 明确需要的运行时 DLL，保留其他文件的来源标记。
    """
    if sys.platform != "win32":
        return False
    if root is None:
        if not getattr(sys, "frozen", False):
            return False
        root = os.path.dirname(os.path.abspath(sys.executable))
    runtime_path = os.path.join(os.fspath(root), *_PYTHONNET_RUNTIME_RELATIVE_PATH)
    if not os.path.isfile(runtime_path):
        return False
    try:
        os.remove(f"{runtime_path}:Zone.Identifier")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


# WebView2 运行时在注册表中的四个安装通道（正式版 / Beta / Dev / Canary）
_WEBVIEW2_CLIENT_KEYS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",
)


def _has_webview2() -> bool:
    """检测 WebView2 运行时是否已安装（仅 Windows 需要）。"""
    if sys.platform != "win32":
        return True
    import winreg

    for client_key in _WEBVIEW2_CLIENT_KEYS:
        for root, prefix in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE"),
        ):
            try:
                with winreg.OpenKey(
                    root, rf"{prefix}\Microsoft\EdgeUpdate\Clients\{client_key}"
                ) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
            except OSError:
                continue
            if version and version != "0.0.0.0":
                return True
    return False


def _warn_missing_webview2() -> None:
    """弹出模态提示，告知需安装 WebView2 运行时。"""
    message = (
        "未检测到 Microsoft Edge WebView2 运行时，程序无法正常显示界面。\n\n"
        "请下载并安装 WebView2Setup.exe 后重试：\n"
        "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "缺少运行环境", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def _install_loading_overlay(window: webview.Window) -> None:
    """WebView2 冷启动期间在窗体上盖一层原生加载画面。

    Windows 后端下客户区完全由 WebView2 控件占据，其内核进程冷启动 + 首次
    导航期间会绘制一块空白画布；把窗体背景设成任何颜色都盖不住它，因为空白
    来自控件自身。这里在控件之上叠加一个原生 ``Label``，用 ``before_show``
    时机装好，等 ``loaded``（页面已绘制）再移除，从而消除启动白屏。
    非 Windows 或后端不同时静默跳过，退化为原有行为。
    """
    try:
        from System import Action
        from System.Drawing import ColorTranslator, ContentAlignment, Font
        from System.Windows.Forms import DockStyle, Label, Timer

        form = window.native
        overlay = Label()
        overlay.Dock = DockStyle.Fill
        overlay.BackColor = ColorTranslator.FromHtml(_BACKGROUND)
        overlay.ForeColor = ColorTranslator.FromHtml("#536471")
        overlay.Font = Font("Microsoft YaHei UI", 11)
        overlay.Text = "正在加载…"
        overlay.TextAlign = ContentAlignment.MiddleCenter
        form.Controls.Add(overlay)
        overlay.BringToFront()

        removed = False

        def _detach() -> None:
            # 仅在 UI 线程执行：WinForms 控件必须在创建它的线程上增删。
            try:
                form.Controls.Remove(overlay)
                overlay.Dispose()
            except Exception:
                pass

        def _remove() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            # pywebview 在后台线程触发 loaded，须切回 UI 线程再动控件。
            try:
                form.BeginInvoke(Action(_detach))
            except Exception:
                _detach()

        # loaded 在 WebView2 的 NavigationCompleted 之后触发，此时页面已绘制，
        # 揭开覆盖层不会露出二次白屏。
        window.events.loaded += _remove

        # 兜底：若 WebView2 初始化失败、始终不来 loaded，覆盖层会在超时后自动
        # 揭开，避免窗口被永久遮挡。Timer 在 UI 线程触发，移除控件是线程安全的。
        watchdog = Timer()
        watchdog.Interval = 15000
        watchdog.Tick += lambda *_: (watchdog.Stop(), _detach())
        watchdog.Start()
    except Exception:
        pass


def main() -> None:
    """创建窗口并进入 pywebview 事件循环。"""
    _unblock_pythonnet_runtime()
    import webview

    if not _has_webview2():
        _warn_missing_webview2()
        return
    bridge = Bridge(Settings())
    window = webview.create_window(
        title=f"对分易自动签到 v{__version__}",
        url=resource_path("web", "index.html"),
        js_api=bridge,
        width=1000,
        height=660,
        min_size=(1000, 660),
        resizable=False,
        # 无边框：去掉系统标题栏与描边，由前端自绘顶栏（含最小化/关闭）。
        frameless=True,
        # easy_drag=False：该开关会在 window 上挂全局 mousedown 监听，导致
        # 整个窗口都能拖动。这里关掉它，改用 pywebview 另一套机制——
        # document.body 上无条件挂载的处理器只认 .pywebview-drag-region 元素，
        # 因此仅顶栏（带该类）可拖，页面其余区域不可拖。
        easy_drag=False,
        background_color=_BACKGROUND,
    )
    bridge.set_window(window)
    window.events.before_show += lambda: _install_loading_overlay(window)
    webview.start()


if __name__ == "__main__":
    main()
