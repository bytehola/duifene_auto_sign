"""二维码图片内容解码（可选依赖 Pillow + pyzbar）。

依赖缺失时抛出 :class:`QrDecodeUnavailable`，由上层给出可操作的安装提示；
本模块不做任何「猜令牌」式的回退。
"""

from __future__ import annotations


class QrDecodeUnavailable(RuntimeError):
    """缺少 Pillow / pyzbar，无法解码二维码图片。"""


def decode_payload(image: bytes) -> str:
    """解码二维码图片，返回其编码的文本；识别不出内容返回空串。

    Raises:
        QrDecodeUnavailable: 运行环境缺少解码依赖。
    """
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError as exc:
        raise QrDecodeUnavailable(str(exc)) from exc

    from io import BytesIO

    results = decode(Image.open(BytesIO(image)))
    for result in results:
        data = result.data
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        if text.strip():
            return text.strip()
    return ""
