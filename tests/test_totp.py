"""驗證器 App 的 6 位數（RFC 6238）。"""

import base64

from church_bot.core import totp

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 附錄 B 的 SHA1 金鑰


def test_matches_the_rfc_test_vectors():
    assert totp.code_at(RFC_SECRET, 59 // 30, digits=8) == "94287082"
    assert totp.code_at(RFC_SECRET, 1111111109 // 30, digits=8) == "07081804"
    assert totp.code_at(RFC_SECRET, 20000000000 // 30, digits=8) == "65353130"


def test_accepts_a_little_clock_drift_but_each_code_only_once():
    now = [1_700_000_000.0]
    verifier = totp.Verifier(clock=lambda: now[0])
    secret = totp.new_secret()
    previous = totp.code_at(secret, totp.now_counter(now[0]) - 1)
    assert verifier.check(secret, previous[:3] + " " + previous[3:])  # 手機慢了一點、中間有空白都可以
    assert not verifier.check(secret, previous)  # 同一組不能再用
    assert not verifier.check(secret, totp.code_at(secret, totp.now_counter(now[0]) - 3))  # 太舊
    assert not verifier.check(secret, "12345") and not verifier.check("", "123456")
    now[0] += 60
    assert verifier.check(secret, totp.code_at(secret, totp.now_counter(now[0])))


def test_setup_link_and_qr_code():
    secret = totp.new_secret()
    uri = totp.otpauth_uri(secret)
    assert uri.startswith("otpauth://totp/") and f"secret={secret}" in uri
    svg = totp.qr_svg(uri)
    assert svg is None or svg.startswith("<svg")  # segno 沒裝就改顯示金鑰
