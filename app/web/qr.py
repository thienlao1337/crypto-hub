"""Rendering the QR code for linking an authenticator app.

The image is passed to the template as a data: string, not as a separate URL: that way
the secret ends up neither in a URL nor in the web server's access log.
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
