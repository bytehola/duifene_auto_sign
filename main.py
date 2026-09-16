"""程序入口：在 pywebview 中启动对分易自动签到。"""

from __future__ import annotations

import os
import sys

import webview

from duifene import __version__
from duifene.bridge import Bridge
from duifene.settings import Settings

# 窗口与加载覆盖层共用的底色，保证揭幕时无跳色。
_BACKGROUND = "#f7f9f9"


def resource_path(*parts: str) -> str:
    """解析随程序分发的静态资源路径，返回绝对路径。

    PyInstaller 冻结后资源被解包到 ``sys._MEIPASS``；源码运行时相对脚本目录。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


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
