from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

import requests
import urllib3
import websocket

# 关闭未验证 HTTPS 告警（与主程序一致：走本地抓包代理、不校验证书）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RS = "\x1e"  # SignalR 记录分隔符

DEFAULT_HUB = "https://wsdf.duifene.com/messageHub"
DEFAULT_ORIGIN = "https://www.duifene.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 服务器 Redis 中 keyId->连接 的映射有效期约 10 分钟，这里用 8 分钟主动重注册
REREGISTER_INTERVAL = 8 * 60
KEEPALIVE_INTERVAL = 10
HANDSHAKE_TIMEOUT = 15


class SignalRError(RuntimeError):
    pass


class DuifeneMessageHub:
    """连接对分易 messageHub 并保持在线，收到推送时回调 ``on_message``。

    ``key_id`` 是注册到服务端的连接标识（扫码登录场景取首页隐藏域
    ``topLogin_hidOnlyId``；其它场景可传任意串，服务端不校验）。
    ``on_message`` 在后台读取线程中执行，切勿做阻塞操作。
    """

    def __init__(
        self,
        key_id: str | None = None,
        *,
        hub_url: str = DEFAULT_HUB,
        origin: str = DEFAULT_ORIGIN,
        user_agent: str = DEFAULT_UA,
        on_message: Callable[[str, list[Any]], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        verify: bool = False,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.key_id = key_id
        self.hub_url = hub_url.rstrip("/")
        self.origin = origin
        self.user_agent = user_agent
        self.on_message = on_message
        self.on_log = on_log or (lambda _msg: None)
        self.verify = verify
        self.proxies = proxies

        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._session.verify = verify
        self._session.trust_env = False
        if proxies:
            self._session.proxies = proxies

        self._ws = None
        self._send_lock = threading.Lock()
        self._invocation_id = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._reconnect_delay = 1.0
        self._reregister_started = False

    # ------------------------------------------------------------------ 底层
    def _log(self, msg: str) -> None:
        self.on_log(msg)

    def negotiate(self, timeout: float = 10.0) -> str:
        """协商并返回 connectionToken。无需 cookie。"""
        url = self.hub_url + "/negotiate?negotiateVersion=1"
        resp = self._session.post(
            url,
            headers={"Origin": self.origin, "Referer": self.origin + "/"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise SignalRError(f"negotiate HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        token = data.get("connectionToken")
        if not token:
            raise SignalRError(f"negotiate 响应缺少 connectionToken: {data}")
        self._log(
            f"已协商 connectionId={data.get('connectionId')} "
            f"transports={[t.get('transport') for t in data.get('availableTransports', [])]}"
        )
        return token

    def _open_ws(self, token: str, timeout: float = 20.0):
        ws_url = self.hub_url.replace("https://", "wss://") + "?id=" + token
        return websocket.create_connection(
            ws_url,
            header=[f"Origin: {self.origin}", f"User-Agent: {self.user_agent}"],
            sslopt={"cert_reqs": ssl.CERT_NONE},
            http_proxy_host=self._proxy_host(),
            http_proxy_port=self._proxy_port(),
            timeout=timeout,
        )

    def _proxy_part(self, scheme: str) -> str | None:
        if not self.proxies:
            return None
        return self.proxies.get(scheme)

    def _proxy_host(self) -> str | None:
        val = self._proxy_part("https") or self._proxy_part("http")
        if not val:
            return None
        without_scheme = val.split("://", 1)[-1]
        return without_scheme.rsplit(":", 1)[0]

    def _proxy_port(self) -> int | None:
        val = self._proxy_part("https") or self._proxy_part("http")
        if not val:
            return None
        without_scheme = val.split("://", 1)[-1]
        try:
            return int(without_scheme.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return None

    def _send_raw(self, text: str) -> None:
        if self._ws is None:
            raise SignalRError("WebSocket 未连接")
        with self._send_lock:
            self._ws.send(text)

    def _send_json(self, obj: dict[str, Any]) -> None:
        self._send_raw(json.dumps(obj, separators=(",", ":")) + RS)

    def invoke(self, target: str, *args: Any, invocation_id: str | None = None) -> str:
        """调用服务端 hub 方法（type=1 Invocation）。"""
        self._invocation_id += 1
        inv_id = invocation_id or str(self._invocation_id)
        self._send_json(
            {
                "type": 1,
                "invocationId": inv_id,
                "target": target,
                "arguments": list(args),
            }
        )
        return inv_id

    # ------------------------------------------------------------------ 生命周期
    def _handshake(self) -> None:
        self._send_json({"protocol": "json", "version": 1})
        # 回执形如 "{}"
        resp = self._ws.recv()
        self._log(f"握手回执: {resp!r}")

    def start(self) -> None:
        """建立连接、注册 keyId 并启动读取线程（非阻塞）。"""
        self._stop.clear()
        self._reregister_started = False
        self._connect_and_register()
        self._spawn(self._ping_loop, "duifene-hub-ping")
        self._spawn(self._reader_loop, "duifene-hub-reader")

    def _connect_and_register(self) -> None:
        token = self.negotiate()
        for attempt in range(6):
            try:
                self._ws = self._open_ws(token)
                break
            except Exception as exc:  # noqa: BLE001 - 失败重试
                self._log(f"WS 连接失败（第 {attempt + 1} 次）：{exc}")
                time.sleep(1.5)
        else:
            raise SignalRError("WebSocket 多次连接失败")
        self._log("WebSocket 已连接")
        self._handshake()
        if self.key_id is not None:
            self.invoke("connect", self.key_id)
            self._log(f"已注册 keyId={self.key_id}")
            if not self._reregister_started:
                self._reregister_started = True
                self._spawn(self._reregister_loop, "duifene-hub-reregister")

    def _spawn(self, fn: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _reregister_loop(self) -> None:
        while not self._stop.wait(REREGISTER_INTERVAL):
            try:
                self.invoke("connect", self.key_id)
                self._log("已重新注册 keyId 映射")
            except Exception as exc:  # noqa: BLE001
                self._log(f"重新注册 keyId 失败：{exc}")

    def _ping_loop(self) -> None:
        while not self._stop.wait(KEEPALIVE_INTERVAL):
            try:
                self._send_json({"type": 6})
            except Exception:  # noqa: BLE001 - 断线由 reader 触发重连
                return

    def _reader_loop(self) -> None:
        reconnect = True
        while not self._stop.is_set() and reconnect:
            try:
                self._read_and_dispatch()
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    return
                self._log(f"连接中断：{exc}")
            # 断线重连（重新协商并重新注册 keyId）
            delay = min(self._reconnect_delay, 30.0)
            if self._stop.wait(delay):
                return
            try:
                self._close_ws()
                self._connect_and_register()
                self._reconnect_delay = 1.0
                self._log("已重新连接并注册")
            except Exception as exc:  # noqa: BLE001
                self._log(f"重连失败：{exc}")
                self._reconnect_delay *= 2

    def _read_and_dispatch(self) -> None:
        while not self._stop.is_set():
            raw = self._ws.recv()
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            for part in raw.split(RS):
                part = part.strip()
                if not part:
                    continue
                self._dispatch(part)

    def _dispatch(self, part: str) -> None:
        try:
            msg = json.loads(part)
        except json.JSONDecodeError:
            self._log(f"非 JSON 消息：{part[:200]}")
            return
        mtype = msg.get("type")
        if mtype == 6:  # Ping
            return
        if mtype == 7:  # Close
            raise SignalRError(f"服务端关闭连接：{msg.get('error')}")
        if mtype == 3:  # 我方调用的 Completion
            if msg.get("error"):
                self._log(f"调用 {msg.get('invocationId')} 出错：{msg['error']}")
            return
        if mtype == 1:  # 服务端调用客户端方法（如 showMessage）
            target = msg.get("target", "")
            args = msg.get("arguments", [])
            self._log(f"收到推送 target={target}")
            if self.on_message:
                try:
                    self.on_message(target, args)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"on_message 回调异常：{exc}")
            return
        self._log(f"未处理的消息类型 type={mtype}：{part[:200]}")

    def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    def stop(self) -> None:
        self._stop.set()
        try:
            self._send_json({"type": 7})  # 优雅关闭
        except Exception:  # noqa: BLE001
            pass
        self._close_ws()

    def run_forever(self) -> None:
        """阻塞式运行，直到 Ctrl+C 或 stop()。"""
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self._log("收到中断，正在关闭")
        finally:
            self.stop()


__all__ = ["DEFAULT_HUB", "DEFAULT_ORIGIN", "DuifeneMessageHub", "SignalRError"]
