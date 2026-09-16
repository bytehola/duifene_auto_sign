"""messageHub WebSocket 推送监听（外部 I/O）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .api import DuifeneApi
from .errors import LogFn
from .settings import Settings
from .signalr import DuifeneMessageHub


class PushMonitor:
    """订阅 messageHub，实时接收他人签到推送并筛选可跟随的坐标。

    注册标识是**签到活动 ID**（非用户身份）。回调运行在 WebSocket 读取线程，
    仅做入队，绝不操作 GUI 控件。

    只在推送回调里输出日志：他人签到的 UserID、经纬度与距离。连接协商、
    握手、重连等通道生命周期信息不记录——它们属实现细节，对用户无价值。
    """

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        own_user_id: Callable[[], str | None],
        log: LogFn | None = None,
    ) -> None:
        """own_user_id 返回本账号学生 ID，用于滤除自己的推送。"""
        self._settings = settings
        self._api = api
        self._own_user_id = own_user_id
        self._log = log or (lambda _message: None)
        self._hub: Any = None
        self._activity_id: str | None = None
        self._queue: list[tuple[float, float, float]] = []

    @property
    def subscribed_id(self) -> str | None:
        """当前订阅的活动 ID。"""
        return self._activity_id

    def subscribe(self, activity_id: str) -> bool:
        """（重新）订阅指定活动；返回是否订阅成功。"""
        self.stop()
        try:
            hub = DuifeneMessageHub(
                key_id=str(activity_id),
                hub_url=self._settings.hub_url,
                on_message=self._on_message,
                on_log=lambda _message: None,
                verify=self._api.verify,
                proxies=self._api.proxies,
            )
            hub.start()
        except Exception:
            self._hub = None
            self._activity_id = None
            return False
        self._hub = hub
        self._activity_id = str(activity_id)
        self._queue.clear()
        return True

    def stop(self) -> None:
        """停止订阅并清空候选坐标。"""
        if self._hub is not None:
            try:
                self._hub.stop()
            except Exception:
                pass
        self._hub = None
        self._activity_id = None
        self._queue.clear()

    def take_follow_point(self) -> tuple[float, float, float] | None:
        """取出一个合格推送坐标 `(经度, 纬度, 距离)`；队列为空返回 None。

        仅监控线程调用；入队发生在 WS 读取线程，CPython 下 list 的
        append/pop 受 GIL 保护，单生产单消费无需额外加锁。
        """
        return self._queue.pop(0) if self._queue else None

    def _on_message(self, target: str, args: list[Any]) -> None:
        """SignalR 推送回调：仅 `showMessage`、非自己、距离达标者入队。

        所有他人签到推送都记一条日志（UserID、经纬度、距离），无论距离是否
        达标——它是判断「有没有人在签到、教室大概在哪」的直接依据。
        """
        if target != "showMessage":
            return
        payload = args[0] if args else ""
        try:
            data = json.loads(payload) if isinstance(payload, str) else dict(payload)
            user_id = str(data.get("UserID") or "")
            longitude = data.get("Longitude")
            latitude = data.get("Latitude")
            distance = float(data.get("Distance") or 0)
        except (ValueError, TypeError):
            return
        if not (longitude and latitude):
            return
        own = self._own_user_id()
        if user_id and own and user_id == str(own):
            return
        self._log(
            f"监听其他用户签到: {user_id} 经度={float(longitude):.6f} "
            f"纬度={float(latitude):.6f} 距离={distance:.1f} 米"
        )
        # 只跟随「距离足够小」的点：他人探测点距离极大，会被此判据滤除；
        # 达标点本身即可签到成功，故复用其坐标最省请求。
        if distance > self._settings.ws_follow_distance_m:
            return
        self._queue.append((float(longitude), float(latitude), distance))
