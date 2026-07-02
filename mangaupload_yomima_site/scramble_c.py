"""
scramble_c.py
=============
ScrambleC: numpy ブロックスクランブル（アップロード時に適用・外部保存前）

シード導出:
    ファイル名 stem の後半8文字（stem[12:20]）と SERVER_SECRET を
    xxhash.xxh64 でハッシュして64bit整数シードを生成する。
    CBZが流出してもファイル名だけからシードを逆算できない。

暗号化フロー（アップロード時）:
    ScrambleA済み画像 → scramble_c_encrypt() → CBZにパック → gigafileへ

復号フロー（視聴時）:
    CBZ展開 → scramble_c_decrypt() → ScrambleB + ウォーターマーク → 配信

依存:
    pip install numpy pillow xxhash
"""

from __future__ import annotations

import itertools
import os

import numpy as np
import xxhash
from PIL import Image

# ---------------------------------------------------------------------------
# 秘密鍵（環境変数から取得）
# ---------------------------------------------------------------------------
SERVER_SECRET = os.environ.get("SCRAMBLE_C_SECRET", "changeme_set_env")

BLOCK_SIZE_DEFAULT = 16


# ---------------------------------------------------------------------------
# シード導出
# ---------------------------------------------------------------------------
def derive_seed_c(filename: str) -> int:
    """
    ファイル名からScrambleCのシードを導出する。

    Parameters
    ----------
    filename : CBZ内ファイル名（例: 0001a1b2c3d4e5f6g7h8.png）

    Returns
    -------
    int : 64bit整数シード
    """
    stem   = os.path.splitext(os.path.basename(filename))[0]
    suffix = stem[12:20]   # stem後半8文字（ScrambleBと重複しない部分）
    return xxhash.xxh64(suffix + SERVER_SECRET).intdigest()


# ---------------------------------------------------------------------------
# 共通: 乱数シーケンス生成
# ---------------------------------------------------------------------------
def _build_rng_sequences(seed: int, num_total_blocks: int, channels: int):
    rng             = np.random.default_rng(seed)
    spatial_indices = np.arange(num_total_blocks)
    rng.shuffle(spatial_indices)
    rot_commands    = rng.integers(0, 4,    size=num_total_blocks)
    invert_commands = rng.integers(0, 2,    size=num_total_blocks)
    perms           = np.array(list(itertools.permutations(range(channels))))
    color_commands  = rng.integers(0, len(perms), size=num_total_blocks)
    return spatial_indices, rot_commands, invert_commands, perms, color_commands


# ---------------------------------------------------------------------------
# 暗号化
# ---------------------------------------------------------------------------
def scramble_c_encrypt(img: Image.Image, seed: int, block_size: int = BLOCK_SIZE_DEFAULT) -> Image.Image:
    """
    ScrambleCで画像を暗号化する。

    Parameters
    ----------
    img        : PIL Image（ScrambleA適用済み）
    seed       : derive_seed_c() で得た64bitシード
    block_size : ブロックサイズ（デフォルト16px）

    Returns
    -------
    Image.Image : 暗号化済み画像
    """
    img_mode  = img.mode
    img_array = np.ascontiguousarray(np.array(img), dtype=np.uint8)
    if img_array.ndim == 2:
        img_array = img_array[..., np.newaxis]

    height, width, channels = img_array.shape
    num_blocks_h  = height // block_size
    num_blocks_w  = width  // block_size
    num_total_blocks = num_blocks_h * num_blocks_w

    spatial_indices, rot_commands, invert_commands, perms, color_commands = \
        _build_rng_sequences(seed, num_total_blocks, channels)

    all_indices = np.arange(num_total_blocks)
    base_ys     = all_indices // num_blocks_w
    base_xs     = all_indices  % num_blocks_w
    src_ys      = base_ys[spatial_indices]
    src_xs      = base_xs[spatial_indices]
    dst_ys      = base_ys
    dst_xs      = base_xs

    active_rots    = rot_commands[spatial_indices]
    active_colors  = color_commands[spatial_indices]
    active_inverts = invert_commands[spatial_indices]
    target_ch      = min(3, channels)

    out_array = img_array.copy()

    for dst_idx in range(num_total_blocks):
        src_by = src_ys[dst_idx]
        src_bx = src_xs[dst_idx]
        dst_by = dst_ys[dst_idx]
        dst_bx = dst_xs[dst_idx]

        block = img_array[
            src_by*block_size:(src_by+1)*block_size,
            src_bx*block_size:(src_bx+1)*block_size,
            :
        ]

        k = active_rots[dst_idx]
        if k > 0:
            block = np.rot90(block, k=k, axes=(0, 1))
        if channels > 1:
            block = block[..., perms[active_colors[dst_idx]]]
        if active_inverts[dst_idx] == 1:
            block = block.copy()
            block[..., :target_ch] = 255 - block[..., :target_ch]

        out_array[
            dst_by*block_size:(dst_by+1)*block_size,
            dst_bx*block_size:(dst_bx+1)*block_size,
            :
        ] = block

    if img_mode == "L" or channels == 1:
        output_array = np.ascontiguousarray(out_array[:, :, 0], dtype=np.uint8)
    else:
        output_array = np.ascontiguousarray(out_array, dtype=np.uint8)

    return Image.fromarray(output_array, mode=img_mode)


# ---------------------------------------------------------------------------
# 復号
# ---------------------------------------------------------------------------
def scramble_c_decrypt(img: Image.Image, seed: int, block_size: int = BLOCK_SIZE_DEFAULT) -> Image.Image:
    """
    ScrambleCで暗号化された画像を復号する。

    Parameters
    ----------
    img        : PIL Image（CBZ展開直後）
    seed       : derive_seed_c() で得た64bitシード
    block_size : 暗号化時と同じブロックサイズ

    Returns
    -------
    Image.Image : 復号済み画像（ScrambleA適用状態に戻る）
    """
    img_mode       = img.mode
    shuffled_array = np.ascontiguousarray(np.array(img), dtype=np.uint8)
    if shuffled_array.ndim == 2:
        shuffled_array = shuffled_array[..., np.newaxis]

    height, width, channels = shuffled_array.shape
    num_blocks_h  = height // block_size
    num_blocks_w  = width  // block_size
    num_total_blocks = num_blocks_h * num_blocks_w

    spatial_indices, rot_commands, invert_commands, perms, color_commands = \
        _build_rng_sequences(seed, num_total_blocks, channels)

    # チャンネル逆置換テーブル
    rev_perms = np.zeros_like(perms)
    for i, perm in enumerate(perms):
        rev_perms[i, perm] = np.arange(channels)

    all_indices = np.arange(num_total_blocks)
    base_ys     = all_indices // num_blocks_w
    base_xs     = all_indices  % num_blocks_w
    src_ys      = base_ys[spatial_indices]
    src_xs      = base_xs[spatial_indices]
    dst_ys      = base_ys
    dst_xs      = base_xs

    active_rots    = rot_commands[spatial_indices]
    active_colors  = color_commands[spatial_indices]
    active_inverts = invert_commands[spatial_indices]
    target_ch      = min(3, channels)

    out_array = shuffled_array.copy()

    for dst_idx in range(num_total_blocks):
        src_by = src_ys[dst_idx]
        src_bx = src_xs[dst_idx]
        dst_by = dst_ys[dst_idx]
        dst_bx = dst_xs[dst_idx]

        block = shuffled_array[
            dst_by*block_size:(dst_by+1)*block_size,
            dst_bx*block_size:(dst_bx+1)*block_size,
            :
        ]

        # リバースパイプライン
        if active_inverts[dst_idx] == 1:
            block = block.copy()
            block[..., :target_ch] = 255 - block[..., :target_ch]
        if channels > 1:
            block = block[..., rev_perms[active_colors[dst_idx]]]
        k = active_rots[dst_idx]
        if k > 0:
            block = np.rot90(block, k=4-k, axes=(0, 1))

        out_array[
            src_by*block_size:(src_by+1)*block_size,
            src_bx*block_size:(src_bx+1)*block_size,
            :
        ] = block

    if img_mode == "L" or channels == 1:
        output_array = np.ascontiguousarray(out_array[:, :, 0], dtype=np.uint8)
    else:
        output_array = np.ascontiguousarray(out_array, dtype=np.uint8)

    return Image.fromarray(output_array, mode=img_mode)