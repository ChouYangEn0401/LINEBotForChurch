"""驗證器 App（Google Authenticator 這類）的 6 位數：RFC 6238 TOTP，只用標準函式庫。

伺服器管理員從外面（免費模式的臨時網址）登入時，管理者密碼之外的第二道（另一個選擇是 Telegram 數字碼，見 login_codes.py）。
* 金鑰存在 .env 的 SERVER_MANAGER_TOTP_SECRET；設定時在網頁上掃 QR code（segno 沒裝的話就顯示金鑰讓人手動輸入）。
* 每 30 秒換一組；前後各容許一組（手機時間差一點也沒關係）。
* 同一組碼用過就不能再用（``Verifier`` 記住用過的時間格），被偷看到也不能拿去再登入一次。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
ISSUER = "服事提醒機器人"


def new_secret() -> str:
    """160 bits 隨機，Base32（驗證器 App 手動輸入用的就是這個）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    clean = secret.replace(" ", "").upper()
    return base64.b32decode(clean + "=" * (-len(clean) % 8))


def code_at(secret: str, counter: int, digits: int = DIGITS) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def now_counter(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def otpauth_uri(secret: str, account: str = "server_manager") -> str:
    label = quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={STEP}"


def qr_svg(text: str) -> str | None:
    """QR code（SVG 字串）。segno 沒裝就回傳 None，畫面改成顯示金鑰讓人手動輸入。"""
    try:
        import segno
    except ImportError:
        return None
    return segno.make(text, error="m").svg_inline(scale=4, dark="#1e222d", light="#ffffff")


class Verifier:
    """檢查驗證器的碼；用過的時間格記在記憶體，同一組碼不能用兩次。"""

    def __init__(self, clock=time.time) -> None:  # noqa: ANN001
        self._clock = clock
        self._lock = threading.Lock()
        self._last_used = -1

    def check(self, secret: str, code: str) -> bool:
        code = "".join(ch for ch in code if ch.isdigit())
        if not secret or len(code) != DIGITS:
            return False
        current = now_counter(self._clock())
        with self._lock:
            for counter in (current - 1, current, current + 1):
                if counter > self._last_used and hmac.compare_digest(code_at(secret, counter), code):
                    self._last_used = counter
                    return True
        return False
