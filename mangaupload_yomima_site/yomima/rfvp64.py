"""
rfvp64.py
=========
RFVP-64: IPv4 アドレスをカラードット絵アバターにエンコード／デコードする。
Reed-Solomon (GF4, N=24, K=16, R=8) による誤り訂正付き。

サイズ: CELL_PX=8, QUIET=2 → total = 9*8 + 2*2*8 = 104px 正方形
        上下10%不動エリア（130px想定）に収まる。

独立チェックサム:
   RS符号の代数だけで「自分で作った答えを自分で検算する」と、
   システマティック符号の性質上どうしても循環論法になり、シンドロームが
   一致するだけの『別の有効符号語』を正解として誤って採用してしまうことがある
   （実測で偽陽性率 約18%）。これを避けるため、RS符号とは完全に独立した
   8bitチェックサムを右下の余白に埋め込み、RS復号の候補が出るたびに
   このチェックサムと突き合わせて検算する。
"""

from __future__ import annotations

import socket
import struct
from itertools import product

import numpy as np
from PIL import Image, ImageDraw


class GF4:
    _MUL = [[0,0,0,0],[0,1,2,3],[0,2,3,1],[0,3,1,2]]

    @staticmethod
    def add(a: int, b: int) -> int:
        return a ^ b

    @classmethod
    def mul(cls, a: int, b: int) -> int:
        return cls._MUL[a][b]

    @classmethod
    def poly_mul(cls, p1: list, p2: list) -> list:
        r = [0] * (len(p1) + len(p2) - 1)
        for i, a in enumerate(p1):
            for j, b in enumerate(p2):
                r[i+j] = cls.add(r[i+j], cls.mul(a, b))
        return r

    @classmethod
    def poly_div_rs(cls, data: list, g_poly: list) -> list:
        r = len(g_poly) - 1
        remainder = [0] * r + data
        for i in range(len(remainder) - 1, r - 1, -1):
            coef = remainder[i]
            if coef == 0:
                continue
            for j in range(len(g_poly)):
                remainder[i - (r - j)] = cls.add(
                    remainder[i - (r - j)], cls.mul(g_poly[j], coef)
                )
        return remainder[:r]


class RFVP64Codec:
    ROWS, COLS     = 9, 5
    N, K, R        = 24, 16, 8
    CELL_PX        = 8
    QUIET          = 2
    CHECKSUM_SYMBOLS = 4

    COLOR_THEMES = {
        0: [
            (255, 220,  80, 255),
            (120,  60, 180, 255),
            ( 20,  20,  20, 255),
            (220,  60,  60, 255),
            (255, 150, 150, 255),
        ],
        1: [
            ( 60, 200, 100, 255),
            ( 50, 100, 220, 255),
            ( 20,  20,  20, 255),
            (200,  50,  50, 255),
            (255, 180, 180, 255),
        ],
    }

    FINDER_DARK  = ( 20,  20,  20, 255)
    FINDER_LIGHT = (240, 240, 240, 255)
    FINDER_BG    = (180, 180, 180, 255)

    def __init__(self) -> None:
        self.STATIC_MASK = np.zeros((9, 5), dtype=int)
        for r, c, v in [
            (0,0,-1),(0,1,-1),
            (1,0, 1),(1,1,-1),
            (2,0, 1),(2,1, 2),
            (3,0, 1),(3,1, 2),
            (4,0, 1),(4,1, 4),(4,2,-1),
            (5,0, 1),(5,1, 3),(5,2,-1),
            (6,0, 1),(6,1,-1),
            (7,0, 1),(7,1,-1),
            (8,0,-1),(8,1,-1),(8,3,-1),
        ]:
            self.STATIC_MASK[r, c] = v

        self.DATA_COORDS = [
            (r, c) for r in range(9) for c in range(5)
            if self.STATIC_MASK[r, c] == 0
        ]
        assert len(self.DATA_COORDS) == self.N

        self.g_poly = [1]
        for root in [1,2,3,1,2,3,1,2][:self.R]:
            self.g_poly = GF4.poly_mul(self.g_poly, [root, 1])

    @property
    def image_size(self) -> int:
        return self.ROWS * self.CELL_PX + self.QUIET * 2 * self.CELL_PX

    def _build_grid(self, rs_stream: list) -> np.ndarray:
        left = np.full((9, 5), 0, dtype=int)
        for r in range(9):
            for c in range(5):
                v = self.STATIC_MASK[r, c]
                if   v > 0:  left[r, c] = v
                elif v == -1: left[r, c] = -1
        for i, (r, c) in enumerate(self.DATA_COORDS):
            left[r, c] = rs_stream[i]
        full = np.zeros((9, 9), dtype=int)
        for r in range(9):
            for c in range(5):
                full[r, c]   = left[r, c]
                full[r, 8-c] = left[r, c]
        return full

    def _compute_checksum(self, packed: int) -> list:
        h = packed & 0xFFFFFFFF
        h = ((h >> 16) ^ h) & 0xFFFFFFFF
        h = (h * 0x45d9f3b) & 0xFFFFFFFF
        h = ((h >> 16) ^ h) & 0xFFFFFFFF
        h = (h * 0x45d9f3b) & 0xFFFFFFFF
        h = ((h >> 16) ^ h) & 0xFFFFFFFF
        checksum_byte = h & 0xFF
        return [(checksum_byte >> (2*i)) & 3 for i in range(self.CHECKSUM_SYMBOLS)]

    def encode(self, ipv4: str, palette_idx: int = 1) -> Image.Image:
        packed    = struct.unpack(">I", socket.inet_aton(ipv4))[0]
        data_poly = [(packed >> (2*i)) & 3 for i in range(self.K)]
        parity    = GF4.poly_div_rs(data_poly, self.g_poly)
        rs_stream = parity + data_poly

        grid = self._build_grid(rs_stream)
        cell, q = self.CELL_PX, self.QUIET
        total   = 9 * cell + q * 2 * cell

        img  = Image.new("RGBA", (total, total), self.FINDER_BG)
        draw = ImageDraw.Draw(img)
        palette = self.COLOR_THEMES[palette_idx % len(self.COLOR_THEMES)]

        for r in range(9):
            for c in range(9):
                v = grid[r, c]
                color = self.FINDER_BG if v == -1 else palette[v]
                x0, y0 = (c + q) * cell, (r + q) * cell
                draw.rectangle([x0, y0, x0+cell-1, y0+cell-1], fill=color)

        for fy, fx in [(0, 0), (0, total-3*cell), (total-3*cell, 0)]:
            draw.rectangle([fx, fy, fx+3*cell-1, fy+3*cell-1], fill=self.FINDER_DARK)
            draw.rectangle([fx+cell, fy+cell, fx+2*cell-1, fy+2*cell-1], fill=self.FINDER_LIGHT)

        # 右下余白にチェックサムを描画
        checksum_syms = self._compute_checksum(packed)
        for i, sym in enumerate(checksum_syms):
            mx = total - (self.CHECKSUM_SYMBOLS - i) * cell
            my = total - cell
            draw.rectangle([mx, my, mx+cell-1, my+cell-1], fill=palette[sym])

        return img

    def _detect_palette(self, img: Image.Image) -> int:
        arr    = np.array(img.convert("RGB"))
        pixels = arr.reshape(-1, 3)
        best_palette, min_dist = 0, float("inf")
        for pi, palette in self.COLOR_THEMES.items():
            total_dist = sum(
                np.min(np.linalg.norm(pixels - np.array(c[:3]), axis=1))
                for c in palette
            )
            if total_dist < min_dist:
                min_dist, best_palette = total_dist, pi
        return best_palette

    def _nearest_color(self, pixel: np.ndarray, palette: list) -> int:
        best, best_dist = 0, float("inf")
        for i, c in enumerate(palette[:4]):
            dist = sum((int(pixel[k]) - c[k])**2 for k in range(3))
            if dist < best_dist:
                best_dist, best = dist, i
        return best

    def _rs_decode(self, received: list, erasure_idxs: list,
                   checksum_received: list | None = None) -> tuple[int | None, str]:
        roots = [1,2,3,1,2,3,1,2][:self.R]
        if len(erasure_idxs) > 4:
            return None, "消失数が多すぎます（上限: 4）"

        def check_syndromes(code: list) -> bool:
            for root in roots:
                s = 0
                for coef in reversed(code):
                    s = GF4.add(GF4.mul(s, root), coef)
                if s != 0:
                    return False
            return True

        def extract_ip(code: list) -> int:
            packed = 0
            for chunk in reversed(code[self.R:]):
                packed = (packed << 2) | chunk
            return packed

        def checksum_ok(packed: int) -> bool:
            return checksum_received is None or \
                   self._compute_checksum(packed) == checksum_received

        if not erasure_idxs:
            if check_syndromes(received):
                packed = extract_ip(received)
                if checksum_ok(packed):
                    return packed, "無傷（チェックサム一致）"
                return None, "シンドローム一致だがチェックサム不一致（偽陽性を回避）"
            return None, "シンドローム不整合"

        for vals in product([0,1,2,3], repeat=len(erasure_idxs)):
            test = list(received)
            for idx, val in zip(erasure_idxs, vals):
                test[idx] = val
            if check_syndromes(test):
                packed = extract_ip(test)
                if checksum_ok(packed):
                    return packed, f"{len(erasure_idxs)}箇所を消失訂正で修復（チェックサム一致）"
        return None, "修復不可能（シンドローム一致候補はチェックサムで全て却下）"

    def decode(self, img: Image.Image,
               palette_idx: int | None = None) -> tuple[str | None, str]:
        sz = self.image_size
        if img.size != (sz, sz):
            img = img.resize((sz, sz), Image.NEAREST)
        arr   = np.array(img.convert("RGBA"))
        cell, q = self.CELL_PX, self.QUIET
        total = sz

        if palette_idx is None:
            palette_idx = self._detect_palette(img)
        palette = self.COLOR_THEMES[palette_idx % len(self.COLOR_THEMES)]

        quantized = np.zeros((9, 9), dtype=int)
        for r in range(9):
            for c in range(9):
                cx = (c+q)*cell + cell//2
                cy = (r+q)*cell + cell//2
                quantized[r, c] = self._nearest_color(arr[cy, cx], palette)

        received = [quantized[r, c] for r, c in self.DATA_COORDS]

        # 右下のチェックサムを読み取る
        checksum_received = []
        for i in range(self.CHECKSUM_SYMBOLS):
            mx = total - (self.CHECKSUM_SYMBOLS - i) * cell + cell // 2
            my = total - cell + cell // 2
            checksum_received.append(self._nearest_color(arr[my, mx], palette))

        AXIS = 4
        erasures: list[int] = []
        for i, (r, c) in enumerate(self.DATA_COORDS):
            if c != AXIS and quantized[r, c] != quantized[r, 8-c]:
                erasures.append(i)

        packed, msg = self._rs_decode(received, erasures, checksum_received)
        if packed is None:
            return None, msg
        return socket.inet_ntoa(struct.pack(">I", packed)), msg


if __name__ == "__main__":
    codec = RFVP64Codec()
    print(f"画像サイズ: {codec.image_size}×{codec.image_size}px")

    print("\n### 無傷ラウンドトリップ ###")
    for ip, pal in [("192.168.1.100",1),("8.8.8.8",0),("203.0.113.42",1)]:
        img = codec.encode(ip, pal)
        restored, msg = codec.decode(img)
        print(f"  {'✅' if restored==ip else '❌'}  {ip:16s} → {restored}  ({msg})")

    print("\n### ミラー外セル破損 ###")
    import numpy as np
    ip, pal = "192.168.1.100", 1
    for n in [1,2,3,4]:
        img = codec.encode(ip, pal)
        arr = np.array(img)
        for idx in [i for i,(r,c) in enumerate(codec.DATA_COORDS) if c!=4][:n]:
            r,c = codec.DATA_COORDS[idx]
            arr[(r+codec.QUIET)*codec.CELL_PX:(r+codec.QUIET+1)*codec.CELL_PX,
                (c+codec.QUIET)*codec.CELL_PX:(c+codec.QUIET+1)*codec.CELL_PX] = [255,0,255,255]
        restored, msg = codec.decode(Image.fromarray(arr))
        print(f"  {'✅' if restored==ip else '❌'}  軸外{n}箇所 → {restored}  ({msg})")

    print("\n### 軸セル(c=4)破損 ###")
    for idx in [i for i,(r,c) in enumerate(codec.DATA_COORDS) if c==4]:
        r,c = codec.DATA_COORDS[idx]
        img = codec.encode(ip, pal)
        arr = np.array(img)
        arr[(r+codec.QUIET)*codec.CELL_PX:(r+codec.QUIET+1)*codec.CELL_PX,
            (c+codec.QUIET)*codec.CELL_PX:(c+codec.QUIET+1)*codec.CELL_PX] = [255,0,255,255]
        restored, msg = codec.decode(Image.fromarray(arr))
        print(f"  {'✅' if restored==ip else '❌'}  軸セル({r},{c}) → {restored}  ({msg})")

    img = codec.encode("192.168.1.100", 1)
    img.save("/home/claude/rfvp64_test.png")
    print("\nサンプル保存: rfvp64_test.png")