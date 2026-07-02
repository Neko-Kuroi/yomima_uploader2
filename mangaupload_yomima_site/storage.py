"""
storage.py
==========
外部ストレージ抽象化レイヤー。

現在の実装: Gigafile.nu
将来: 別ストレージへの差し替えはこのモジュールのみ変更で対応可能。

暗号化:
    AES-256-GCM で URL と削除キーを暗号化してDBに保存。
    キーは環境変数 STORAGE_ENCRYPT_KEY（32バイト hex, 64文字）から取得。

依存:
    pip install cryptography
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# 暗号化キー（環境変数から取得）
# ---------------------------------------------------------------------------
_ENCRYPT_KEY_HEX = os.environ.get("STORAGE_ENCRYPT_KEY", "")

def _get_key() -> bytes:
    """環境変数から32バイトのAESキーを取得する。"""
    if not _ENCRYPT_KEY_HEX or len(_ENCRYPT_KEY_HEX) != 64:
        raise RuntimeError(
            "環境変数 STORAGE_ENCRYPT_KEY が未設定または不正にゃ。"
            "secrets.token_hex(32) で生成した64文字のhex文字列を設定してにゃ。"
        )
    return bytes.fromhex(_ENCRYPT_KEY_HEX)


# ---------------------------------------------------------------------------
# AES-256-GCM 暗号化 / 復号
# ---------------------------------------------------------------------------
def encrypt_value(plain: str) -> str:
    """
    文字列をAES-256-GCMで暗号化してbase64文字列で返す。

    フォーマット: base64(nonce[12] + ciphertext)
    """
    key   = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct     = aesgcm.encrypt(nonce, plain.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt_value(cipher: str) -> str:
    """
    encrypt_value() で暗号化した文字列を復号して返す。
    """
    key    = _get_key()
    raw    = base64.b64decode(cipher)
    nonce  = raw[:12]
    ct     = raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


# ---------------------------------------------------------------------------
# ストレージ状態定数
# ---------------------------------------------------------------------------
STORAGE_TYPE_LOCAL    = "local"
STORAGE_TYPE_GIGAFILE = "gigafile"

STORAGE_STATUS_ACTIVE  = "active"
STORAGE_STATUS_EXPIRED = "expired"
STORAGE_STATUS_DELETED = "deleted"
STORAGE_STATUS_ERROR   = "error"


# ---------------------------------------------------------------------------
# DB操作ヘルパー
# ---------------------------------------------------------------------------
def store_gigafile_result(
    episode,
    url: str,
    del_key: Optional[str],
    expires_at: datetime,
    db,
) -> None:
    """
    gigafileアップロード結果をDBに保存する。

    Parameters
    ----------
    episode    : SQLAlchemy Episode モデルインスタンス
    url        : gigafileダウンロードページURL
    del_key    : 削除キー（取得できなかった場合は None）
    expires_at : 有効期限
    db         : SQLAlchemy Session
    """
    episode.storage_type               = STORAGE_TYPE_GIGAFILE
    episode.storage_status             = STORAGE_STATUS_ACTIVE
    episode.storage_base_url_encrypted = encrypt_value(url)
    episode.storage_del_key_encrypted  = encrypt_value(del_key) if del_key else None
    episode.storage_expires_at         = expires_at
    db.commit()


def get_storage_url(episode) -> Optional[str]:
    """
    Episodeモデルからストレージ URL を復号して返す。
    取得できない場合は None。
    """
    if not episode.storage_base_url_encrypted:
        return None
    try:
        return decrypt_value(episode.storage_base_url_encrypted)
    except Exception:
        return None


def get_del_key(episode) -> Optional[str]:
    """
    Episodeモデルから削除キーを復号して返す。
    """
    if not episode.storage_del_key_encrypted:
        return None
    try:
        return decrypt_value(episode.storage_del_key_encrypted)
    except Exception:
        return None


def is_expired(episode) -> bool:
    """有効期限切れかどうかを判定する。"""
    if episode.storage_expires_at is None:
        return False
    now = datetime.now(tz=timezone.utc)
    expires = episode.storage_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return now > expires


def mark_deleted(episode, db) -> None:
    """
    削除済みとしてDBを更新する。
    URLと削除キーはセキュリティのためNULLに。
    """
    episode.storage_status             = STORAGE_STATUS_DELETED
    episode.storage_base_url_encrypted = None
    episode.storage_del_key_encrypted  = None
    db.commit()


def mark_expired(episode, db) -> None:
    """期限切れとしてDBを更新する。"""
    episode.storage_status = STORAGE_STATUS_EXPIRED
    db.commit()


def is_available(episode) -> bool:
    """
    コンテンツが視聴可能かどうかを判定する。
    """
    if episode.storage_type == STORAGE_TYPE_LOCAL:
        return True
    return episode.storage_status == STORAGE_STATUS_ACTIVE and not is_expired(episode)