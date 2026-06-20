"""
watermark.py
============
配信時（seed_B確定後）に不動エリア（上下10%）へ
QRコードとヒエログリフ文字列を埋め込む透かしモジュール。

埋め込みタイミング:
    元画像 → scramble(seed_A) → scramble(seed_B) → embed_watermarks() → 配信

位置はすべて seed_B から派生した Xorshift32 で決定するため再現可能。
ヒエログリフはフォント未インストール時でも QR のみで動作する。
"""

from __future__ import annotations

import socket
import struct
from pathlib import Path
from typing import NamedTuple

from PIL import Image

from qr_mark import make_qr_mark
from hiero_mark import make_hiero_mark, HIERO_FONT_PATH
from rfvp64 import RFVP64Codec

_rfvp64 = RFVP64Codec()  # モジュールレベルで1度だけ初期化


# ═══════════════════════════════════════════════════════════════════════
# Xorshift32（manga.py と同一実装）
# ═══════════════════════════════════════════════════════════════════════

class Xorshift32:
    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFF_FFFF or 1

    def next(self) -> int:
        s = self.state
        s ^= (s << 13) & 0xFFFF_FFFF
        s ^= (s >> 17) & 0xFFFF_FFFF
        s ^= (s << 5)  & 0xFFFF_FFFF
        self.state = s & 0xFFFF_FFFF
        return self.state

    def randint(self, n: int) -> int:
        return self.next() % n


# ═══════════════════════════════════════════════════════════════════════
# IP ↔ seed_B 変換（manga.py と同一実装）
# ═══════════════════════════════════════════════════════════════════════

def ip_to_seed_b(ip: str) -> int:
    packed = socket.inet_aton(ip)
    val = struct.unpack("!I", packed)[0]
    result = 0
    for _ in range(32):
        result = (result << 1) | (val & 1)
        val >>= 1
    return result


def seed_b_to_ip(seed_b: int) -> str:
    val = 0
    for _ in range(32):
        val = (val << 1) | (seed_b & 1)
        seed_b >>= 1
    return socket.inet_ntoa(struct.pack("!I", val))


# ═══════════════════════════════════════════════════════════════════════
# 配置ユーティリティ
# ═══════════════════════════════════════════════════════════════════════

class _PlacedRect(NamedTuple):
    x: int
    y: int
    w: int
    h: int


def _overlaps(a: _PlacedRect, b: _PlacedRect, padding: int = 4) -> bool:
    return not (
        a.x + a.w + padding <= b.x or
        b.x + b.w + padding <= a.x or
        a.y + a.h + padding <= b.y or
        b.y + b.h + padding <= a.y
    )


def _place_in_band(
    rng: Xorshift32,
    item_w: int,
    item_h: int,
    band_x0: int,
    band_y0: int,
    band_x1: int,
    band_y1: int,
    placed: list[_PlacedRect],
    margin: int = 6,
    max_tries: int = 40,
) -> _PlacedRect | None:
    max_x = band_x1 - item_w - margin
    max_y = band_y1 - item_h - margin
    if max_x < band_x0 + margin or max_y < band_y0 + margin:
        return None
    for _ in range(max_tries):
        x = band_x0 + margin + rng.randint(max(1, max_x - band_x0 - margin))
        y = band_y0 + margin + rng.randint(max(1, max_y - band_y0 - margin))
        rect = _PlacedRect(x, y, item_w, item_h)
        if not any(_overlaps(rect, p) for p in placed):
            return rect
    return None


# ═══════════════════════════════════════════════════════════════════════
# メイン API
# ═══════════════════════════════════════════════════════════════════════

def embed_watermarks(
    img: Image.Image,
    seed_b: int,
    client_ip: str,
    *,
    qr_count: int = 3,
    qr_scale: int = 3,
    qr_opacity: float = 0.50,
    text_font_size: int = 22,
    text_opacity: float = 1.0,
    text_color: tuple[int, int, int] = (0, 0, 0),
    border_ratio: float = 0.10,
    font_path: Path = HIERO_FONT_PATH,
) -> Image.Image:
    """
    scramble(seed_B) 適用済み画像の不動エリア（上下 border_ratio）に透かしを埋め込む。

    ヒエログリフフォントが未インストールの場合は QR のみ埋め込んで続行する。

    Parameters
    ----------
    img        : scramble(seed_B) 後の画像
    seed_b     : ip_to_seed_b(client_ip) で得た値
    client_ip  : クライアントの IPv4 アドレス文字列
    qr_count   : QR を配置する個数
    qr_scale   : QR モジュール 1つあたりの px
    qr_opacity : QR の不透明度
    text_font_size : ヒエログリフのフォントサイズ（px）
    text_opacity   : ヒエログリフの不透明度
    text_color     : ヒエログリフの RGB カラー
    border_ratio   : 不動エリアの割合（デフォルト 0.10 = 上下10%）
    font_path      : Noto Sans Egyptian Hieroglyphs の .ttf パス
    """
    result = img.convert("RGBA")
    W, H = result.size

    band_h      = int(H * border_ratio)
    top_band    = (0, 0, W, band_h)
    bottom_band = (0, H - band_h, W, H)

    rng    = Xorshift32(seed_b ^ 0xC0FFEE42)
    placed: list[_PlacedRect] = []

    # ── RFVP-64 アバターを上下バンドのいずれかに配置 ─────────────────
    rfvp_img = _rfvp64.encode(client_ip)
    rw, rh   = rfvp_img.size
    for band in [top_band, bottom_band]:
        rect = _place_in_band(rng, rw, rh, *band, placed)
        if rect is not None:
            placed.append(rect)
            result.alpha_composite(rfvp_img, dest=(rect.x, rect.y))
            break

    # ── QR を qr_count 個配置（上下バンドに交互に） ──────────────────
    qr_mark = make_qr_mark(seed_b, scale=qr_scale, opacity=qr_opacity)
    qw, qh  = qr_mark.size

    for i in range(qr_count):
        band = top_band if i % 2 == 0 else bottom_band
        rect = _place_in_band(rng, qw, qh, *band, placed)
        if rect is None:
            continue
        placed.append(rect)
        result.alpha_composite(qr_mark, dest=(rect.x, rect.y))

    # ── ヒエログリフ配置（フォントなし環境はスキップ） ───────────────
    try:
        hiero_mark = make_hiero_mark(
            client_ip,
            font_size=text_font_size,
            opacity=text_opacity,
            color=text_color,
            font_path=font_path,
        )
        tw, th = hiero_mark.size
        for band in [bottom_band, top_band]:
            rect = _place_in_band(rng, tw, th, *band, placed)
            if rect is not None:
                placed.append(rect)
                result.alpha_composite(hiero_mark, dest=(rect.x, rect.y))
                break
    except OSError as e:
        import logging
        logging.getLogger(__name__).warning("ヒエログリフをスキップ: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════════════
# 動作確認
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from hiero_mark import ip_to_hiero, hiero_to_ip

    TEST_IP  = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    TEST_IMG = sys.argv[2] if len(sys.argv) > 2 else None

    seed_b = ip_to_seed_b(TEST_IP)
    hiero  = ip_to_hiero(TEST_IP)

    print(f"IP      : {TEST_IP}")
    print(f"seed_B  : {seed_b:#010x}")
    print(f"hiero   : {hiero}  ({len(hiero)} chars)")
    print(f"QR data : {hex(seed_b)[2:].upper().zfill(8)}")

    assert hiero_to_ip(hiero) == TEST_IP
    assert seed_b_to_ip(seed_b) == TEST_IP
    print("往復テスト: OK")

    if TEST_IMG:
        out_path = Path(TEST_IMG).stem + "_watermarked.png"
        with Image.open(TEST_IMG) as base:
            result = embed_watermarks(base, seed_b, TEST_IP)
        result.save(out_path, "PNG", compress_level=6)
        print(f"保存: {out_path}")
    else:
        dummy  = Image.new("RGB", (1200, 1800), (255, 255, 255))
        result = embed_watermarks(dummy, seed_b, TEST_IP)
        result.save("/home/claude/watermark_test.png", "PNG", compress_level=6)
        print("テスト画像保存: /home/claude/watermark_test.png")