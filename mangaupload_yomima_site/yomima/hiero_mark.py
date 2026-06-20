"""
hiero_mark.py
=============
IPv4 アドレスをヒエログリフ 9文字にエンコード／デコードし、
RGBA Image として描画するモジュール。

外部依存: Pillow, Noto Sans Egyptian Hieroglyphs フォント
フォントがない環境では OSError が発生する。
インストール: sudo apt-get install -y fonts-noto
"""

from __future__ import annotations

import socket
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── フォントパス ──────────────────────────────────────────────────────
HIERO_FONT_PATH = Path(
    "/usr/share/fonts/truetype/noto/NotoSansEgyptianHieroglyphs-Regular.ttf"
)

# ── ヒエログリフ変換テーブル（5bit × 32文字）────────────────────────
HIERO_WORDS = [
    "𓄿", "𓀀", "𓆗", "𓇹", "𓉐", "𓊛", "𓆣", "𓋹",  # 0〜7
    "𓏏", "𓅓", "𓃠", "𓁺", "𓀬", "𓋔", "𓭬", "𓆏",  # 8〜15
    "𓃗", "𓅱", "𓎛", "𓏲", "𓍝", "𓇋", "𓐍", "𓏠",  # 16〜23
    "𓈖", "𓌨", "𓎡", "𓄲", "𓂝", "𓌕", "𓄭", "𓊝",  # 24〜31
]
HIERO_TO_VAL = {ch: i for i, ch in enumerate(HIERO_WORDS)}


# ═══════════════════════════════════════════════════════════════════════
# Fletcher チェックサム（5bit 版）
# ═══════════════════════════════════════════════════════════════════════

def _fletcher32_5bit(chunks: list[int]) -> tuple[int, int]:
    s1 = s2 = 0
    for v in chunks:
        s1 = (s1 + v) % 32
        s2 = (s2 + s1) % 32
    return s1, s2


# ═══════════════════════════════════════════════════════════════════════
# エンコード／デコード
# ═══════════════════════════════════════════════════════════════════════

def ip_to_hiero(ip: str) -> str:
    """
    IPv4 アドレスを 9文字のヒエログリフ文字列にエンコード。
    7文字データ + 2文字 Fletcher チェックサム。
    """
    val = struct.unpack("!I", socket.inet_aton(ip))[0]
    chunks: list[int] = []
    tmp = val
    for _ in range(7):
        chunks.append(tmp & 0x1F)
        tmp >>= 5
    chunks.reverse()
    ck1, ck2 = _fletcher32_5bit(chunks)
    chunks += [ck1, ck2]
    return "".join(HIERO_WORDS[c] for c in chunks)


def hiero_to_ip(text: str) -> str:
    """
    9文字のヒエログリフ文字列を IPv4 アドレスにデコード。
    チェックサム不一致の場合は ValueError を送出する。
    """
    chars = list(text.strip().replace(" ", ""))
    if len(chars) != 9:
        raise ValueError(f"9文字必要ですが {len(chars)} 文字です")
    try:
        vals = [HIERO_TO_VAL[c] for c in chars]
    except KeyError as e:
        raise ValueError(f"未知のヒエログリフ: {e}") from e
    data, ck1_given, ck2_given = vals[:7], vals[7], vals[8]
    ck1_calc, ck2_calc = _fletcher32_5bit(data)
    if ck1_given != ck1_calc or ck2_given != ck2_calc:
        raise ValueError("チェックサム不一致：文字列が破損または改ざんされています")
    result = 0
    for v in data:
        result = (result << 5) | v
    return socket.inet_ntoa(struct.pack("!I", result))


# ═══════════════════════════════════════════════════════════════════════
# 描画
# ═══════════════════════════════════════════════════════════════════════

def make_hiero_mark(
    ip: str,
    font_size: int = 22,
    opacity: float = 1.0,
    color: tuple[int, int, int] = (0, 0, 0),
    font_path: Path = HIERO_FONT_PATH,
) -> Image.Image:
    """
    IPv4 アドレスをヒエログリフ 9文字に変換して RGBA Image を返す。

    Parameters
    ----------
    ip        : クライアントの IPv4 アドレス文字列
    font_size : フォントサイズ（px）
    opacity   : 文字の不透明度
    color     : 文字の RGB カラー
    font_path : Noto Sans Egyptian Hieroglyphs の .ttf パス

    Raises
    ------
    OSError
        フォントファイルが見つからない場合。
        （呼び出し元で try/except して graceful degradation 可能）
    """
    try:
        font = ImageFont.truetype(str(font_path), font_size)
    except OSError:
        raise OSError(
            f"フォントが見つかりません: {font_path}\n"
            "sudo apt-get install -y fonts-noto を実行してください"
        )

    text = ip_to_hiero(ip)

    dummy = Image.new("RGBA", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    bbox  = draw.textbbox((0, 0), text, font=font)
    tw    = bbox[2] - bbox[0] + 2
    th    = bbox[3] - bbox[1] + 2

    mark = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    draw.text(
        (-bbox[0] + 1, -bbox[1] + 1),
        text,
        font=font,
        fill=(*color, int(255 * opacity)),
    )
    return mark