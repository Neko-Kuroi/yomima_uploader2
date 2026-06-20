"""
qr_mark.py
==========
seed_B を QR コードに変換して RGBA Image を返すモジュール。
外部依存: Pillow, segno
"""

from __future__ import annotations

import io

from PIL import Image
import segno


def make_qr_mark(seed_b: int, scale: int = 3, opacity: float = 0.50) -> Image.Image:
    """
    seed_B を 8桁 hex にエンコードした QR コードを生成する。

    白背景付きで生成するため、元画像の色に関わらず
    QR 内部の白黒コントラストが保たれ読み取り精度が安定する。

    Parameters
    ----------
    seed_b  : ip_to_seed_b(client_ip) で得た uint32
    scale   : QR モジュール 1つあたりの px（default 3 → 約57px）
    opacity : QR 全体（白背景含む）の不透明度

    Returns
    -------
    Image.Image  RGBA の QR 画像
    """
    data = hex(seed_b)[2:].upper().zfill(8)  # 例: "548E00D3"
    qr   = segno.make(data, error="L", boost_error=False)

    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, dark="#000000", light="#ffffff")
    buf.seek(0)

    mark  = Image.open(buf).convert("RGBA")
    alpha = mark.getchannel("A")
    lut   = [int(i * opacity) for i in range(256)]
    mark.putalpha(alpha.point(lut))
    return mark


def qr_data_from_seed_b(seed_b: int) -> str:
    """seed_B を QR に埋め込む文字列に変換（デコード側との対称用）"""
    return hex(seed_b)[2:].upper().zfill(8)