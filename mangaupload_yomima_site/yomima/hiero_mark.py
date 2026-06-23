from __future__ import annotations

import struct
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── フォントパス・タイムゾーン設定 ────────────────────────────────────
HIERO_FONT_PATH = Path(__file__).parent / "fonts" / "NotoSansEgyptianHieroglyphs-Regular.ttf"
JST = timezone(timedelta(hours=9))  # 日本時間

# ── ヒエログリフ変換テーブル（5bit × 32文字）────────────────────────
HIERO_WORDS = [
    "𓄿", "𓀀", "𓆗", "𓇹", "𓉐", "𓊛", "𓆣", "𓋹",  # 0〜7
    "𓏏", "𓅓", "𓃠", "𓁺", "𓀬", "𓋔", "𓭬", "𓆏",  # 8〜15
    "𓃗", "𓅱", "𓎛", "𓏲", "𓍝", "𓇋", "𓐍", "𓏠",  # 16〜23
    "𓈖", "𓌨", "𓎡", "𓄲", "𓂝", "𓌕", "𓄭", "𓊝",  # 24〜31
]
HIERO_TO_VAL = {ch: i for i, ch in enumerate(HIERO_WORDS)}


def _fletcher32_5bit(chunks: list[int]) -> tuple[int, int]:
    s1 = s2 = 0
    for v in chunks:
        s1 = (s1 + v) % 32
        s2 = (s2 + s1) % 32
    return s1, s2


# ═══════════════════════════════════════════════════════════════════════
# エンコード／デコード
# ═══════════════════════════════════════════════════════════════════════

def datetime_to_hiero(dt: datetime) -> str:
    """
    datetime オブジェクトを 10文字のヒエログリフにエンコード。
    """
    timestamp = int(dt.timestamp())
    
    if timestamp < 0 or timestamp >= (1 << 40):
        raise ValueError("対応していない日時範囲です。")

    chunks: list[int] = []
    for i in range(7, -1, -1):
        chunks.append((timestamp >> (i * 5)) & 0x1F)
    
    ck1, ck2 = _fletcher32_5bit(chunks)
    chunks += [ck1, ck2]
    
    return "".join(HIERO_WORDS[c] for c in chunks)


def hiero_to_datetime(text: str) -> datetime:
    """
    10文字のヒエログリフ文字列を datetime オブジェクト（JST）にデコード。
    """
    chars = list(text.strip().replace(" ", ""))
    if len(chars) != 10:
        raise ValueError(f"10文字必要ですが {len(chars)} 文字です")
        
    try:
        vals = [HIERO_TO_VAL[c] for c in chars]
    except KeyError as e:
        raise ValueError(f"未知のヒエログリフ: {e}") from e
        
    data, ck1_given, ck2_given = vals[:8], vals[8], vals[9]
    
    ck1_calc, ck2_calc = _fletcher32_5bit(data)
    if ck1_given != ck1_calc or ck2_given != ck2_calc:
        raise ValueError("チェックサム不一致：文字列が破損または改ざんされています")
        
    timestamp = 0
    for v in data:
        timestamp = (timestamp << 5) | v
        
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(JST)


# ═══════════════════════════════════════════════════════════════════════
# 描画
# ═══════════════════════════════════════════════════════════════════════

def make_hiero_date_mark(
    dt: datetime,
    font_size: int = 22,
    opacity: float = 1.0,
    color: tuple[int, int, int] = (0, 0, 0),
    font_path: Path = HIERO_FONT_PATH,
) -> Image.Image:
    """
    日時データをヒエログリフ 10文字に変換して RGBA Image を返す。
    """
    try:
        font = ImageFont.truetype(str(font_path), font_size)
    except OSError:
        raise OSError(f"フォントが見つかりません: {font_path}")

    text = datetime_to_hiero(dt)

    dummy = Image.new("RGBA", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    bbox  = draw.textbbox((0, 0), text, font=font)
    
    tw    = bbox[2] - bbox[0] + 4
    th    = bbox[3] - bbox[1] + 4

    mark = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    
    draw.text(
        (2 - bbox[0], 2 - bbox[1]),
        text,
        font=font,
        fill=(*color, int(255 * opacity)),
    )
    return mark