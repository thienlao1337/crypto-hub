"""Отрисовка QR-кода для привязки приложения-аутентификатора.

Картинка отдаётся строкой data: прямо в шаблон, а не отдельным адресом:
так секрет не попадает ни в URL, ни в журнал доступа веб-сервера.
"""

import base64
import io

import qrcode


def data_uri(payload: str) -> str:
    image = qrcode.make(payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
