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


def make_qr_mark(
    seed_b: int | None,
    scale: int = 3,
    opacity: float = 0.50,
    client_ip: str | None = None,
) -> Image.Image:
    """
    seed_B を 8桁 hex にエンコードした QR コードを生成する。
    IPv6クライアントの場合は client_ip を 32桁 hex で直接埋め込む。

    白背景付きで生成するため、元画像の色に関わらず
    QR 内部の白黒コントラストが保たれ読み取り精度が安定する。

    Parameters
    ----------
    seed_b     : ip_to_seed_b(client_ip) で得た uint32。IPv6の場合は None。
    scale      : QR モジュール 1つあたりの px（default 3 → 約57px）
    opacity    : QR 全体（白背景含む）の不透明度
    client_ip  : IPv6の場合に直接埋め込むIPアドレス文字列

    Returns
    -------
    Image.Image  RGBA の QR 画像
    """
    if seed_b is not None:
        data = hex(seed_b)[2:].upper().zfill(8)   # IPv4: 例 "548E00D3"
    else:
        # IPv6: 128bitをhex32文字で埋め込む
        import ipaddress
        data = format(int(ipaddress.ip_address(client_ip)), '032X')
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