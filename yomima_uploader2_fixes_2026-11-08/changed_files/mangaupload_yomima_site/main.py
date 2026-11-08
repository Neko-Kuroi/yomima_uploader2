import os
import re
import uuid
import json
import time
import shutil
import threading
import zipfile
import logging
import secrets
import asyncio
import base64
import hashlib
import hmac as hmac_lib
import numpy as np
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, UTC
from typing import List, Generator, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, BackgroundTasks, Query, Header
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from jose import jwt, JWTError
from PIL import Image
from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime, UniqueConstraint, text, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship, joinedload
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import Response

# ---------------------------------------------------------------------------
# ログ設定
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ディレクトリ準備
# ---------------------------------------------------------------------------
STATIC_DIR    = Path("./static")
TMP_DIR       = Path("./storage/tmp")
FINAL_ZIP_DIR = Path("./storage/zips")
for d in [STATIC_DIR, TMP_DIR, FINAL_ZIP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
# 甲-1 (案ア): 署名鍵は環境変数 SECRET_KEY から読む。直書き・既定値への
# フォールバックはしない。未設定・空文字なら起動時に分かりやすく停止する。
SECRET_KEY                  = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError(
        "環境変数 SECRET_KEY が設定されていません。"
        "作者ログイン用トークンの署名鍵を、環境変数 SECRET_KEY に"
        "十分に長いランダムな文字列を設定してから起動してください。"
        "例: SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    )
ALGORITHM                   = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
MAX_PAGE_SIZE_MB             = 10
MAX_TOTAL_SIZE_MB            = 100
MAX_COVER_SIZE_MB            = 5
MAX_PAGE_SIZE_BYTES          = MAX_PAGE_SIZE_MB  * 1024 * 1024
MAX_TOTAL_SIZE_BYTES         = MAX_TOTAL_SIZE_MB * 1024 * 1024
MAX_COVER_SIZE_BYTES         = MAX_COVER_SIZE_MB * 1024 * 1024
DISPLAY_MAX_SIZE             = (1200, 1800)
PADDING_CANVAS_SIZE          = (720, 1080)   # DISPLAY_MAX_SIZEの60%
PADDING_THRESHOLD            = (600, 900)    # これ以下の場合パディング適用
THUMB_SIZE                   = (150, 220)
TILE_SIZE_DEFAULT            = 16
# 丙-2: tile_size の入口範囲。既定値 16 を含み、実利用 (16〜32 程度) を
# 十分に覆う範囲。これより小さいとタイル数が爆発し、大きいと
# ページ寸法 (最大 1200x1800) に対して意味を成さない。
TILE_SIZE_MIN                = 4
TILE_SIZE_MAX                = 256
# N-14: 画素数の上限。表示は最大 1200x1800 に縮小され、画面側の案内も
# 幅1440px・高さ2160px のため、実用原稿はその数倍で十分に収まる。
# 2500万画素 (例: 4000x6000 のスキャン) までを許し、それを超える
# 「バイト数は小さいのに展開すると巨大」な画像を入口で止める。
MAX_IMAGE_PIXELS             = 25_000_000
MAX_IMAGE_DIMENSION          = 8192
Image.MAX_IMAGE_PIXELS       = MAX_IMAGE_PIXELS
CHARSET_62                   = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

def validate_tile_size(tile_size: int) -> None:
    """丙-2: tile_size が範囲外なら入口で 400 にする (ジョブエラーにしない)。"""
    if not (TILE_SIZE_MIN <= tile_size <= TILE_SIZE_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"tile_size は {TILE_SIZE_MIN}〜{TILE_SIZE_MAX} の範囲で指定してください")

def normalize_tile_size(value, default: int = TILE_SIZE_DEFAULT) -> int:
    """保存済み設定など、入口を過ぎた値の防御用正規化。

    範囲外・解釈できない値は既定値に落とす (配信側を落とさないため)。
    入口での拒否は validate_tile_size の役割。
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if TILE_SIZE_MIN <= v <= TILE_SIZE_MAX else default

# ---------------------------------------------------------------------------
# CBZアクセス制御
# ---------------------------------------------------------------------------
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")
CBZ_TOKEN_SECRET = secrets.token_hex(32)   # 起動時ランダム生成・外部に出ない
CBZ_TOKEN_TTL    = 300                      # 5分
_VIEWER_BASE_URL_ENV = os.environ.get("VIEWER_BASE_URL", "")

def get_viewer_base_url() -> str:
    """viewer_url.txt があればそちらを優先、なければ環境変数、最後にlocalhost"""
    url_file = Path("./viewer_url.txt")
    if url_file.exists():
        val = url_file.read_text().strip()
        if val.startswith("http"):
            return val
    if _VIEWER_BASE_URL_ENV:
        return _VIEWER_BASE_URL_ENV
    return "http://localhost:8001"


def _origin_of(url: str) -> tuple[str, str, int] | None:
    """URL を (scheme, host, port) の組に正規化する。読めなければ None。

    ポート省略時はスキームの既定ポート (http=80 / https=443) を補う。
    """
    from urllib.parse import urlparse
    try:
        p = urlparse(url if "://" in url else f"http://{url}")
        if not p.hostname:
            return None
        scheme = (p.scheme or "http").lower()
        port = p.port or (443 if scheme == "https" else 80)
        return (scheme, p.hostname.lower(), port)
    except Exception:
        return None


def _referer_is_viewer(referer: str | None, viewer_base: str) -> bool:
    """Referer の出所 (scheme+host+port) がビューワーと完全一致するか (丙-6)。

    以前は文字列の前方一致だったため、ビューワーの URL の後ろに
    文字列を足しただけの別ホスト (例: localhost:8001.evil.com) でも
    通ってしまった。パス部分は見ない (ビューワー内のどの画面から
    来てもよい)。
    """
    if not referer:
        return False
    a, b = _origin_of(referer), _origin_of(viewer_base)
    return a is not None and b is not None and a == b


def _mask_token(token: str | None) -> str:
    """ログ用にトークンを伏せる (丙-6)。先頭4文字と長さだけを残す。"""
    if not token:
        return "(none)"
    return f"{token[:4]}…(len={len(token)})"

# nonce使い捨て管理 { nonce: 登録時刻 }
_used_cbz_nonces: dict[str, float] = {}

def _consume_nonce(nonce: str) -> bool:
    """未使用nonceならTrueを返して使用済み登録。使用済みはFalse。"""
    now = time.time()
    expired = [k for k, v in _used_cbz_nonces.items() if now - v > 600]
    for k in expired:
        del _used_cbz_nonces[k]
    if nonce in _used_cbz_nonces:
        return False
    _used_cbz_nonces[nonce] = now
    return True

def _make_cbz_token(public_id: str) -> str:
    """CBZ専用署名トークンを生成する"""
    payload = {
        "pid":   public_id,
        "exp":   int(time.time()) + CBZ_TOKEN_TTL,
        "nonce": secrets.token_hex(8),
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
    sig = hmac_lib.new(
        CBZ_TOKEN_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).hexdigest()[:24]
    return f"{payload_b64}.{sig}"

def _verify_cbz_token(token: str, public_id: str) -> bool:
    """CBZトークンを多重検証する（HMAC・exp・public_id・nonce使い捨て）"""
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        return False
    expected = hmac_lib.new(
        CBZ_TOKEN_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).hexdigest()[:24]
    if not hmac_lib.compare_digest(sig, expected):
        return False
    padding = 4 - len(payload_b64) % 4
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * padding)
        )
    except Exception:
        return False
    # Y-3: exp や nonce が欠けたトークンは「検証失敗」として False を返す
    # (以前は KeyError が素通りして 500 になっていた)
    try:
        exp = payload["exp"]
    except (KeyError, TypeError):
        return False
    if not isinstance(exp, (int, float)) or time.time() > exp:
        return False
    if payload.get("pid") != public_id:
        return False
    nonce = payload.get("nonce")
    if not nonce or not _consume_nonce(nonce):
        return False
    return True

# ---------------------------------------------------------------------------
# プレビュー専用トークン (N-13)
# ログイン JWT そのものを URL に載せるのをやめ、プレビューだけに使える
# 短命 (10分) のトークンを別に発行する。sub / pid / purpose を入れ、
# 他の作品や作者 API 一般には使えない形にする。
# ---------------------------------------------------------------------------
PREVIEW_TOKEN_TTL_MINUTES = 10

def _make_preview_token(user_id: int, public_id: str) -> str:
    return jwt.encode(
        {
            "sub":     str(user_id),
            "pid":     public_id,
            "purpose": "preview",
            "exp":     datetime.now(UTC) + timedelta(minutes=PREVIEW_TOKEN_TTL_MINUTES),
        },
        SECRET_KEY, algorithm=ALGORITHM,
    )

def _verify_preview_token(token: str, public_id: str) -> int:
    """プレビュー専用トークンを検証し、作者の user_id を返す。

    失敗はすべて 401 (丙-10: 数字でない sub でも 500 にしない)。
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="プレビュートークンが無効または期限切れです")
    if payload.get("purpose") != "preview" or payload.get("pid") != public_id:
        raise HTTPException(status_code=401, detail="プレビュートークンが無効です")
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="プレビュートークンが無効です")

# ---------------------------------------------------------------------------
# データベース
# ---------------------------------------------------------------------------
DATABASE_URL = "sqlite:///./storage/manga_platform.db"
engine       = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()

class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String,  unique=True, index=True, nullable=False)
    email           = Column(String,  unique=True, index=True, nullable=False)
    hashed_password = Column(String,  nullable=False)
    is_active       = Column(Boolean, default=True)
    works           = relationship("Work", back_populates="author")

class Work(Base):
    __tablename__ = "works"
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title       = Column(String,  nullable=False)
    description = Column(String,  default="")
    created_at  = Column(DateTime, default=datetime.utcnow)
    author      = relationship("User", back_populates="works")
    episodes    = relationship("Episode", back_populates="work")

class Episode(Base):
    __tablename__ = "episodes"
    id          = Column(Integer, primary_key=True, index=True)
    work_id     = Column(Integer, ForeignKey("works.id"), nullable=False, index=True)
    public_id   = Column(String,  unique=True, index=True, nullable=True)
    title       = Column(String,  nullable=False)
    rating      = Column(String,  default="General")
    status      = Column(String,  default="draft")
    scrambled   = Column(Boolean, default=False)
    view_count  = Column(Integer, default=0)
    cbz_path    = Column(String,  default="")
    feedback    = Column(String,  default="")
    created_at  = Column(DateTime, default=datetime.utcnow)
    work        = relationship("Work", back_populates="episodes")
    tags        = relationship("EpisodeTag", back_populates="episode")
    warnings    = relationship("EpisodeWarning", back_populates="episode")
    reactions   = relationship("Reaction", back_populates="episode")

class Tag(Base):
    __tablename__ = "tags"
    id       = Column(Integer, primary_key=True, index=True)
    name     = Column(String,  nullable=False, index=True)
    tag_type = Column(String,  nullable=False)
    episodes = relationship("EpisodeTag", back_populates="tag")

class EpisodeTag(Base):
    __tablename__ = "episode_tags"
    episode_id = Column(Integer, ForeignKey("episodes.id"), primary_key=True)
    tag_id     = Column(Integer, ForeignKey("tags.id"),     primary_key=True)
    episode    = relationship("Episode", back_populates="tags")
    tag        = relationship("Tag",     back_populates="episodes")

class EpisodeWarning(Base):
    __tablename__ = "episode_warnings"
    id           = Column(Integer, primary_key=True, index=True)
    episode_id   = Column(Integer, ForeignKey("episodes.id"), nullable=False, index=True)
    warning_type = Column(String,  nullable=False)
    episode      = relationship("Episode", back_populates="warnings")

class Reaction(Base):
    __tablename__ = "reactions"
    id            = Column(Integer, primary_key=True, index=True)
    episode_id    = Column(Integer, ForeignKey("episodes.id"), nullable=False, index=True)
    reaction_type = Column(String,  nullable=False)
    client_ip     = Column(String,  nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
    episode       = relationship("Episode", back_populates="reactions")
    __table_args__ = (
        UniqueConstraint("episode_id", "reaction_type", "client_ip", name="uq_reaction"),
    )

GENRE_TAGS = [
    "寓話", "不条理", "私小説", "記録", "幻想", "叙事", "断片",
    "コメディ", "ギャグ", "ほのぼの", "恐怖", "風刺", "叙情", "シュール",
    "日常", "歴史", "SF", "ファンタジー", "民話的", "魔術的リアリズム",
    "冒険", "活劇", "ピカレスク",
    "学園", "グルメ", "ギャンブル", "スポーツ", "アイドル",
    "政治",
    "家族", "友情", "恋愛", "群像", "動物",
    "少女", "少年", "青年", "劇画",
    "エッチ",
]
WARNING_TAGS = ["暴力", "性暴力", "自傷", "死"]

def init_tags(db: Session) -> None:
    for name in GENRE_TAGS:
        if not db.query(Tag).filter_by(name=name, tag_type="genre").first():
            db.add(Tag(name=name, tag_type="genre"))
    for name in WARNING_TAGS:
        if not db.query(Tag).filter_by(name=name, tag_type="warning").first():
            db.add(Tag(name=name, tag_type="warning"))
    db.commit()

Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------------------------
# マイグレーション
# ---------------------------------------------------------------------------
def run_migrations() -> None:
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("PRAGMA table_info(episodes)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "public_id" not in existing_cols:
            cur.execute("ALTER TABLE episodes ADD COLUMN public_id VARCHAR")
            con.commit()
            logger.info("Migration: episodes.public_id added")
        if "feedback" not in existing_cols:
            cur.execute("ALTER TABLE episodes ADD COLUMN feedback VARCHAR DEFAULT ''")
            con.commit()
            logger.info("Migration: episodes.feedback added")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ix_episodes_public_id
            ON episodes (public_id)
        """)
        con.commit()
        con.close()
    except Exception as e:
        logger.warning(f"Migration warning: {e}")

run_migrations()

_db = SessionLocal()
try:
    init_tags(_db)
finally:
    _db.close()

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------------------------------------------------------
# settings.json の読み書き (丙-4)
#
# 従来は「開いてそのまま書く」だけだったため、同時に更新すると片方が
# 消えたり、書き込み中に止まると壊れた JSON が残って、その作品が
# 一覧から黙って消えたりしていた。ここに読み書きを寄せる。
# - 書き込み: 一時ファイルに書いてから os.replace で置き換える
#   (原子的な置き換え)。プロセス内はロックで直列化する。
# - 読み込み: ファイルが無い場合は None、壊れている場合は
#   SettingsCorruptError を送出する。呼び出し側は「黙って飛ばす」の
#   ではなく、配信なら fail-closed、一覧なら退避+ログで表に出す。
# ---------------------------------------------------------------------------
_settings_io_lock = threading.Lock()

class SettingsCorruptError(Exception):
    """settings.json が JSON として読めなかったことを表す。"""

def read_settings_json(path) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SettingsCorruptError(f"{p}: {e}") from e
    if not isinstance(data, dict):
        raise SettingsCorruptError(f"{p}: 先頭がオブジェクトではありません")
    return data

def write_settings_json(path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    with _settings_io_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

def backup_corrupt_settings(path) -> Optional[Path]:
    """壊れた settings.json を別名で退避し、ログに残す (丙-4)。"""
    p = Path(path)
    if not p.exists():
        return None
    backup = p.with_name(p.name + f".corrupt-{int(time.time() * 1000)}")
    try:
        os.replace(p, backup)
        logger.error(
            f"settings.json が壊れていたため退避しました: {p} -> {backup}")
        return backup
    except OSError as e:
        logger.error(f"settings.json の退避に失敗しました: {p}: {e}")
        return None

def read_settings_for_listing(path) -> Optional[dict]:
    """一覧表示用に読む。壊れていたら退避して表に出し、その作品だけ除く。"""
    try:
        return read_settings_json(path)
    except SettingsCorruptError as e:
        logger.error(f"settings.json の読み込みに失敗しました: {e}")
        backup_corrupt_settings(path)
        return None

def load_episode_settings_strict(settings_path: Path) -> dict:
    """配信可否の判定に使う読み方。無い・壊れている場合は配信しない。"""
    try:
        meta = read_settings_json(settings_path)
    except SettingsCorruptError as e:
        logger.error(f"設定ファイルが壊れているため配信しません: {e}")
        raise HTTPException(status_code=500,
                            detail="設定ファイルが壊れています")
    if meta is None:
        raise HTTPException(status_code=404,
                            detail="設定ファイルが見つかりません")
    return meta

def require_public_delivery(ep: "Episode", settings_path: Path) -> dict:
    """公開受け口 (CBZ・サムネイル・カバー) で渡してよい話かを確認する。

    乙-5 / 乙-6 / N-6 の共通部品。次のすべてを満たす場合だけ配信する:
    - DB と設定ファイルの両方で status が published
    - access_level が public
    access_level の取りうる値は public (無料公開) と premium
    (有料会員限定) だが、現行コードには有料会員の認証基盤が存在しない
    ため、premium を「誰に渡してよいか」を判定する材料が無い。
    したがって public 以外は公開受け口では渡さず、所有者と
    プレビュー (作者用受け口) だけが例外になる。
    """
    meta = load_episode_settings_strict(settings_path)
    if ep.status != "published" or meta.get("status") != "published":
        raise HTTPException(status_code=403, detail="この作品は非公開です")
    if meta.get("access_level", "public") != "public":
        raise HTTPException(status_code=403,
                            detail="この作品は公開範囲が制限されています")
    return meta

def assert_title_episode_available(db: Session, user: "User",
                                   title_id: int, episode_id: int) -> None:
    """乙-3: 作品番号・話番号の衝突と他人の番号の使用を 409 で断る。

    作品番号と話番号は DB の主キーでもあるため、他人が既に使っている
    番号を指定すると主キー衝突で 500 になっていた。また自分の作品の
    番号でなければ、他人の作品を指す指定もできないようにする。
    """
    work = db.query(Work).filter_by(id=title_id).first()
    if work is not None and work.user_id != user.id:
        raise HTTPException(status_code=409,
                            detail="その作品番号は既に使われています")
    ep = db.query(Episode).filter_by(id=episode_id).first()
    if ep is not None and ep.work_id != title_id:
        raise HTTPException(status_code=409,
                            detail="そのエピソード番号は既に使われています")

# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------
pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="認証トークンが無効です")
    except JWTError:
        raise HTTPException(status_code=401, detail="トークンの期限切れ、または不正です")
    # 丙-10: sub が数字でない場合は 500 でなく 401 にする
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="認証トークンが無効です")
    user = db.query(User).filter(User.id == user_id_int).first()
    if not user:
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
    # 乙-1: 停止中のアカウントは、発行済みトークンでも使えない
    if not user.is_active:
        raise HTTPException(status_code=403, detail="このアカウントは停止されています")
    return user

# ---------------------------------------------------------------------------
# Pydantic スキーマ
# ---------------------------------------------------------------------------
class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class EpisodePublishSettings(BaseModel):
    status:          str
    access_level:    str
    price:           int        = 0
    title_name:      str        = ""
    episode_name:    str        = ""
    caption:         str        = ""
    comment:         str        = ""
    note:            str        = ""
    feedback:        str        = ""
    scrambled:       bool       = True
    tile_size:       int        = TILE_SIZE_DEFAULT
    webhook_enabled: bool       = False
    rating:          str        = "General"
    warnings:        List[str]  = []
    genre_tags:      List[str]  = []
    motif_tags:      List[str]  = []

# ---------------------------------------------------------------------------
# タグ同期ヘルパー
# ---------------------------------------------------------------------------
def sync_episode_tags(
    db: Session,
    user: User,
    title_id: int,
    episode_id: int,
    s: "EpisodePublishSettings",
) -> None:
    work = db.query(Work).filter_by(user_id=user.id, id=title_id).first()
    if not work:
        work = Work(id=title_id, user_id=user.id, title=s.title_name or f"title_{title_id}")
        db.add(work)
        db.flush()
    else:
        if s.title_name:
            work.title = s.title_name

    ep = db.query(Episode).filter_by(id=episode_id, work_id=title_id).first()
    if not ep:
        ep = Episode(
            id        = episode_id,
            work_id   = title_id,
            public_id = uuid.uuid4().hex,
            title     = s.episode_name or f"ep_{episode_id}",
        )
        db.add(ep)
        db.flush()
    # public_id は新規作成時のみ生成、以降は上書きしない
    ep.title     = s.episode_name or ep.title
    ep.rating    = s.rating
    ep.status    = s.status
    ep.scrambled = s.scrambled
    ep.feedback  = s.feedback

    db.query(EpisodeWarning).filter_by(episode_id=episode_id).delete()
    for w in s.warnings:
        db.add(EpisodeWarning(episode_id=episode_id, warning_type=w))

    db.query(EpisodeTag).filter_by(episode_id=episode_id).delete()
    for name in s.genre_tags:
        tag = db.query(Tag).filter_by(name=name, tag_type="genre").first()
        if tag:
            db.add(EpisodeTag(episode_id=episode_id, tag_id=tag.id))
    for name in s.motif_tags:
        tag = db.query(Tag).filter_by(name=name, tag_type="motif").first()
        if not tag:
            tag = Tag(name=name, tag_type="motif")
            db.add(tag)
            db.flush()
        db.add(EpisodeTag(episode_id=episode_id, tag_id=tag.id))

    try:
        db.commit()
    except IntegrityError:
        # 乙-3: 同時実行などで主キー衝突が起きても 500 にせず 409 にする
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="その作品番号またはエピソード番号は既に使われています")

# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def trust_cf_connecting_ip() -> bool:
    """甲-6: CF-Connecting-IP を信じてよいのは、Cloudflare 経由の運用で
    環境変数 TRUST_CF_CONNECTING_IP=1 (true/yes/on) を明示した場合だけ。
    それ以外 (直接アクセス) ではヘッダは利用者が自由に付けられるため信じない。
    ビューワー側 (yomima/main.py) と同じ決め方に揃えている。"""
    return os.environ.get("TRUST_CF_CONNECTING_IP", "").strip().lower() in (
        "1", "true", "yes", "on")

def get_client_ip(request: Request) -> str:
    if trust_cf_connecting_ip():
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip
    return request.client.host if request.client else "unknown"

def natural_keys(text: str):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

def to_base62(n: int, width: int = 4) -> str:
    if n == 0:
        return "0".zfill(width)
    res = ""
    while n > 0:
        n, r = divmod(n, 62)
        res = CHARSET_62[r] + res
    return res.zfill(width)

def generate_page_filename(index: int) -> str:
    prefix      = to_base62(index, width=4)
    random_part = secrets.token_hex(8)
    return f"{prefix}{random_part}.png"

class Xorshift32:
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF
        if self.state == 0:
            self.state = 1

    def next(self) -> int:
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17) & 0xFFFFFFFF
        x ^= (x << 5)  & 0xFFFFFFFF
        self.state = x & 0xFFFFFFFF
        return self.state

def xorshift_shuffle(items: list, seed: int) -> list:
    rng = Xorshift32(seed)
    res = items[:]
    for i in range(len(res) - 1, 0, -1):
        j = _rand_int(rng, i + 1)
        res[i], res[j] = res[j], res[i]
    return res

def _rand_int(rng: Xorshift32, n: int) -> int:
    """rejection samplingで偏りなし均等整数を返す"""
    limit = (1 << 32) - ((1 << 32) % n)
    while True:
        r = rng.next()
        if r < limit:
            return r % n

def _make_perm(n: int, seed: int) -> np.ndarray:
    """rejection samplingでnumpyの順列配列を生成"""
    rng  = Xorshift32(seed)
    perm = np.arange(n, dtype=np.int32)
    for i in range(n - 1, 0, -1):
        j = _rand_int(rng, i + 1)
        perm[i], perm[j] = perm[j], perm[i]
    return perm

def scramble_image(img: Image.Image, seed: int, tile_size: int) -> Image.Image:
    """
    中央80%エリアのタイルを完全ベクトル化で高速スクランブルする。
    上下左右10%は不動エリアとして保持。
    """
    arr = np.array(img)
    h, w = arr.shape[:2]
    cols = w // tile_size
    rows = h // tile_size

    x0 = int(cols * 0.1)
    x1 = int(cols * 0.9)
    y0 = int(rows * 0.1)
    y1 = int(rows * 0.9)

    cx = x1 - x0
    cy = y1 - y0
    n  = cy * cx

    # 中央エリアをタイル単位の4D配列に変形
    center = arr[y0*tile_size:y1*tile_size, x0*tile_size:x1*tile_size]
    tiled  = (center
              .reshape(cy, tile_size, cx, tile_size, -1)
              .transpose(0, 2, 1, 3, 4)
              .reshape(n, tile_size, tile_size, -1))

    # シャッフル: NumPyインデックス1回で完了
    perm     = _make_perm(n, seed)
    shuffled = tiled[perm]

    # 元の形に戻して結果配列に書き込む
    result = arr.copy()
    result[y0*tile_size:y1*tile_size, x0*tile_size:x1*tile_size] = (
        shuffled
        .reshape(cy, cx, tile_size, tile_size, -1)
        .transpose(0, 2, 1, 3, 4)
        .reshape(cy * tile_size, cx * tile_size, -1)
    )
    return Image.fromarray(result)

def generate_thumbnail(img_path: Path, thumb_dir: Path, index: int) -> Path:
    thumb_path = thumb_dir / f"thumb_{index:04d}.jpg"
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        img.save(thumb_path, "JPEG", quality=80)
    return thumb_path

# ---------------------------------------------------------------------------
# アップロード進捗ストア
# ---------------------------------------------------------------------------
upload_progress: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# ビューカウント用IPクールダウン
# ---------------------------------------------------------------------------
_view_cooldown: dict[str, float] = {}
VIEW_COOLDOWN_SECONDS = 3600

def increment_view_count(settings_path: Path, client_ip: str,
                         episode_id: int | None = None) -> None:
    """N-5: view_count の正本は DB (Episode.view_count) に決める。

    CBZ 取得時に DB 側を +1 し、settings.json 側には DB の値をそのまま
    書き戻して常に一致させる。カタログも DB の値を読むため、管理画面
    (DB) とカタログ (従来は JSON) で数が割れることがなくなる。
    """
    cooldown_key = f"{settings_path}:{client_ip}"
    now = time.time()
    if now - _view_cooldown.get(cooldown_key, 0) < VIEW_COOLDOWN_SECONDS:
        return
    new_count = None
    if episode_id is not None:
        db = SessionLocal()
        try:
            ep = db.query(Episode).filter_by(id=episode_id).first()
            if ep is not None:
                ep.view_count = (ep.view_count or 0) + 1
                db.commit()
                new_count = ep.view_count
        except Exception as e:
            db.rollback()
            logger.warning(f"view_count DB更新失敗: {e}")
        finally:
            db.close()
    try:
        # 丙-4: 読み書きは共通ヘルパー経由 (原子的な置き換え)
        meta = read_settings_json(settings_path)
        if meta is not None:
            # DB が正本。DB で数えられなかった場合だけ従来どおり JSON を +1 する
            meta["view_count"] = (
                new_count if new_count is not None
                else meta.get("view_count", 0) + 1)
            write_settings_json(settings_path, meta)
        _view_cooldown[cooldown_key] = now
    except Exception as e:
        logger.warning(f"view_count更新失敗: {e}")
        if new_count is not None:
            # DB だけでも数えた場合はクールダウンを記録し、二重加算を防ぐ
            _view_cooldown[cooldown_key] = now

def update_progress(job_id: str, phase: str, current: int, total: int, **kwargs):
    # N-21: 記録の所有者は最初の呼び出しで確定し、以降の更新でも保持する
    # (この関数は毎回 dict を作り直すため、引き継がないと消えてしまう)
    owner_id = kwargs.pop("owner_id", None)
    if owner_id is None:
        owner_id = upload_progress.get(job_id, {}).get("owner_id")
    record = {"phase": phase, "current": current, "total": total, **kwargs}
    if owner_id is not None:
        record["owner_id"] = owner_id
    upload_progress[job_id] = record

# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------
def get_ip_for_limit(request: Request) -> str:
    return get_client_ip(request)

limiter = Limiter(key_func=get_ip_for_limit)

# ---------------------------------------------------------------------------
# リクエストボディサイズ制限ミドルウェア (丙-1)
#
# 従来は Content-Length ヘッダだけを見ていたため、ヘッダが無い
# (チャンク転送) 場合は素通りし、数字でないヘッダでは int() の例外が
# 500 になっていた。ここでは:
# - 数字でない Content-Length は 400、申告が上限超えなら 413
# - ヘッダの有無にかかわらず、実際に届いたバイト数を数えながら読み、
#   上限を超えた時点で 413 にしてアプリ本体へは渡さない
# という形にする。数えるために本体を一度メモリへ受けるが、
# アップロード処理側 (_process_upload へ全ファイルを bytes で渡す
# 既存設計) も全読み込みのため、メモリ使用の性質は変わらない。
# ---------------------------------------------------------------------------
async def _send_plain_response(send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})

class LimitUploadSizeMiddleware:
    """純粋 ASGI ミドルウェア (BaseHTTPMiddleware ではない)。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http"
                or "upload-and-zip" not in scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        limit = MAX_TOTAL_SIZE_BYTES + 5 * 1024 * 1024
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_cl = headers.get(b"content-length")
        if raw_cl is not None:
            try:
                declared = int(raw_cl.decode("latin-1").strip())
            except ValueError:
                await _send_plain_response(
                    send, 400, "Content-Length が正しくありません")
                return
            if declared > limit:
                await _send_plain_response(
                    send, 413, "リクエストサイズが上限を超えています")
                return

        body_parts: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if chunk:
                total += len(chunk)
                if total > limit:
                    await _send_plain_response(
                        send, 413, "リクエストサイズが上限を超えています")
                    return
                body_parts.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(body_parts)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)

# ---------------------------------------------------------------------------
# FastAPI アプリ
# ---------------------------------------------------------------------------
app = FastAPI()
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(LimitUploadSizeMiddleware)
# 乙-7: CORS は全開 ("*" + 認証情報許可) にしない。
# 画面 (index.html / admin.html) はこのアプリ自身が配っており、
# 通常運用は同一オリジンで完結するため、既定では CORS ヘッダ自体を
# 出さない。プラットフォームと画面を別オリジンで動かす運用がある
# 場合は、環境変数 CORS_ALLOWED_ORIGINS (カンマ区切り) に許可する
# オリジンを明示した場合だけ、そのオリジンに限って許可する。
_cors_allowed_origins = [
    o.strip()
    for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
if _cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return Response("リクエストが多すぎます。しばらく待ってから再試行してください。", status_code=429)

# ---------------------------------------------------------------------------
# ヘルパー：public_id → (Episode, user_dir, settings_path)
# serve_public_cbz より前に定義する必要がある
# ---------------------------------------------------------------------------
def resolve_episode_by_public_id(public_id: str, db: Session):
    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    work = db.query(Work).filter_by(id=ep.work_id).first()
    if not work:
        raise HTTPException(status_code=404, detail="作品が見つかりません")
    user_dir      = FINAL_ZIP_DIR / f"user_{work.user_id}" / f"title_{work.id}"
    settings_path = user_dir / f"episode_{ep.id}_settings.json"
    return ep, user_dir, settings_path

# ---------------------------------------------------------------------------
# 認証エンドポイント
# ---------------------------------------------------------------------------

# 丙-3: 登録時の検査。管理者画面で表示される名前でもあり、N-2 の
# ユーザー名経由 XSS の入口でもあるため、使える文字を絞る。
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_USERNAMES = {
    "admin", "administrator", "root", "system", "support", "moderator",
    "yomima", "platform", "official", "staff", "null", "undefined",
}
PASSWORD_MIN_LENGTH = 8

@app.post("/api/auth/signup")
@limiter.limit("3/hour")
async def signup(request: Request, user_data: UserRegister, db: Session = Depends(get_db)):
    username = user_data.username
    if not (3 <= len(username) <= 32) or not USERNAME_PATTERN.match(username):
        raise HTTPException(
            400, "ユーザー名は3〜32文字で、英数字と _ - だけが使えます")
    if username.lower() in RESERVED_USERNAMES:
        raise HTTPException(400, "そのユーザー名は使えません")
    if len(user_data.password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(
            400, f"パスワードは{PASSWORD_MIN_LENGTH}文字以上にしてください")
    # 重複チェックは大文字小文字を区別しない。メールは小文字化して保存する
    # (既存データに大文字混じりのメールがあっても、照合側も小文字同士で比べる)
    email_normalized = user_data.email.lower()
    if db.query(User).filter(func.lower(User.email) == email_normalized).first():
        raise HTTPException(400, "登録済みのメールアドレスです")
    if db.query(User).filter(func.lower(User.username) == username.lower()).first():
        raise HTTPException(400, "使用済みのユーザー名です")
    hashed_pw = pwd_context.hash(user_data.password)
    db.add(User(username=username, email=email_normalized, hashed_password=hashed_pw))
    db.commit()
    return {"message": "ユーザー登録が完了しました。ログインしてください。"}

@app.post("/api/auth/token")
@limiter.limit("5/minute")
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(401, "ユーザー名またはパスワードが正しくありません")
    # 乙-1: 停止中のアカウントには新しいトークンを発行しない
    if not user.is_active:
        raise HTTPException(403, "このアカウントは停止されています")
    access_token = jwt.encode(
        {"sub": str(user.id), "exp": datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
        SECRET_KEY, algorithm=ALGORITHM
    )
    return {"access_token": access_token, "token_type": "bearer", "username": user.username}

# ---------------------------------------------------------------------------
# 作者：自分の作品一覧
# ---------------------------------------------------------------------------

@app.get("/api/author/works")
async def get_my_works(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    works    = []
    user_dir = FINAL_ZIP_DIR / f"user_{current_user.id}"
    if not user_dir.exists():
        return []
    for settings_path in sorted(user_dir.glob("title_*/episode_*_settings.json")):
        # 丙-4: 壊れた設定は退避して表に出す (read_settings_for_listing 内)
        meta = read_settings_for_listing(settings_path)
        if meta is None:
            continue
        try:
            cbz_path   = settings_path.parent / f"episode_{meta['episode_id']}.cbz"
            cover_path = settings_path.parent / f"episode_{meta['episode_id']}_cover.jpg"
            meta["has_cbz"]   = cbz_path.exists()
            meta["has_cover"] = cover_path.exists()
            ep = db.query(Episode).filter_by(
                id=meta["episode_id"], work_id=meta["title_id"]
            ).first()
            meta["public_id"] = ep.public_id if ep else None
            # N-5: 作者一覧の閲覧数も正本 (DB) の値で出す
            if ep is not None:
                meta["view_count"] = ep.view_count or 0
            works.append(meta)
        except Exception:
            continue
    return works

# ---------------------------------------------------------------------------
# 作者：アップロード開始
# ---------------------------------------------------------------------------

@app.post("/api/author/titles/{title_id}/episodes/{episode_id}/upload-and-zip")
async def upload_manga(
    title_id:         int,
    episode_id:       int,
    background_tasks: BackgroundTasks,
    files:            List[UploadFile] = File(...),
    cover:            Optional[UploadFile] = File(default=None),
    do_scramble:      bool = True,
    tile_size:        int  = TILE_SIZE_DEFAULT,
    current_user:     User = Depends(get_current_user),
    db:               Session = Depends(get_db),
):
    # 乙-3: 他人の既存番号との衝突は、ファイルを読み込む前に 409 で断る
    if title_id < 1 or episode_id < 1:
        raise HTTPException(
            status_code=400,
            detail="作品番号とエピソード番号は1以上で指定してください")
    assert_title_episode_available(db, current_user, title_id, episode_id)
    # 丙-2: tile_size の範囲外はジョブエラーではなく入口で 400
    validate_tile_size(tile_size)

    total_size = 0
    for f in files:
        content = await f.read()
        await f.seek(0)
        size = len(content)
        if size > MAX_PAGE_SIZE_BYTES:
            raise HTTPException(400, f"{f.filename} が {MAX_PAGE_SIZE_MB}MB を超えています")
        total_size += size
        if total_size > MAX_TOTAL_SIZE_BYTES:
            raise HTTPException(400, f"合計サイズが {MAX_TOTAL_SIZE_MB}MB を超えています")

    if cover:
        cover_content = await cover.read()
        await cover.seek(0)
        if len(cover_content) > MAX_COVER_SIZE_BYTES:
            raise HTTPException(400, f"バンプ画像が {MAX_COVER_SIZE_MB}MB を超えています")

    job_id = uuid.uuid4().hex
    # N-21: 進捗は開始した作者本人だけが引けるよう、所有者を記録する
    update_progress(job_id, "queued", 0, len(files), owner_id=current_user.id)

    file_contents = []
    for f in files:
        content = await f.read()
        file_contents.append((f.filename, content))

    cover_content  = None
    cover_filename = None
    if cover:
        cover_content  = await cover.read()
        cover_filename = cover.filename

    background_tasks.add_task(
        _process_upload,
        job_id, title_id, episode_id,
        file_contents, cover_content, cover_filename,
        do_scramble, tile_size, current_user.id,
    )
    return {"job_id": job_id, "total_files": len(files)}

def _process_upload(
    job_id:         str,
    title_id:       int,
    episode_id:     int,
    file_contents:  list,
    cover_content:  Optional[bytes],
    cover_filename: Optional[str],
    do_scramble:    bool,
    tile_size:      int,
    user_id:        int,
):
    user_dir       = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir      = user_dir / f"episode_{episode_id}_thumbs"
    thumb_dir.mkdir(exist_ok=True)
    final_zip_path = user_dir / f"episode_{episode_id}.cbz"
    work_dir       = TMP_DIR / f"u{user_id}_t{title_id}_e{episode_id}_{uuid.uuid4().hex[:6]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        file_contents.sort(key=lambda x: natural_keys(x[0]))
        total       = len(file_contents)
        saved_pages = []

        for index, (filename, content) in enumerate(file_contents):
            # N-16: フォルダ付きのファイル名でも落ちないよう、
            # 最後の名前の部分だけを使う
            filename = Path(filename.replace("\\", "/")).name
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue

            update_progress(job_id, "resize", index + 1, total)

            raw_path = work_dir / f"raw_{filename}"
            with open(raw_path, "wb") as f:
                f.write(content)

            with Image.open(raw_path) as img:
                # N-14: 画素を展開する前に、寸法で上限を確認する。
                # バイト数が小さくても展開すると巨大になる画像を止める。
                w0, h0 = img.size
                if (w0 * h0 > MAX_IMAGE_PIXELS
                        or max(w0, h0) > MAX_IMAGE_DIMENSION):
                    raise ValueError(
                        f"{filename} の画素数が上限を超えています ({w0}x{h0})")
                img = img.convert("RGBA")
                w, h = img.size
                # 小さすぎる画像はパディングキャンバスにセンター配置
                if w <= PADDING_THRESHOLD[0] or h <= PADDING_THRESHOLD[1]:
                    canvas = Image.new("RGBA", PADDING_CANVAS_SIZE, (0, 0, 0, 0))
                    offset_x = (PADDING_CANVAS_SIZE[0] - w) // 2
                    offset_y = (PADDING_CANVAS_SIZE[1] - h) // 2
                    canvas.paste(img, (offset_x, offset_y))
                    img = canvas
                img.thumbnail(DISPLAY_MAX_SIZE, Image.LANCZOS)
                img = img.convert("RGB")
                resized_path = work_dir / f"resized_{index:04d}.png"
                img.save(resized_path, "PNG")

            generate_thumbnail(resized_path, thumb_dir, index)

            update_progress(job_id, "scramble", index + 1, total)

            page_filename = generate_page_filename(index)
            page_path     = work_dir / page_filename

            with Image.open(resized_path) as img:
                img = img.convert("RGB")
                if do_scramble:
                    seed_hex = page_filename[4:12]
                    seed     = int(seed_hex, 16)
                    img      = scramble_image(img, seed, tile_size)
                img.save(page_path, "PNG")

            saved_pages.append(page_path)
            raw_path.unlink()
            resized_path.unlink()

        update_progress(job_id, "zip", 0, len(saved_pages))
        with zipfile.ZipFile(final_zip_path, 'w', zipfile.ZIP_STORED) as comic_zip:
            for i, p in enumerate(saved_pages):
                update_progress(job_id, "zip", i + 1, len(saved_pages))
                comic_zip.write(p, arcname=p.name)

        if cover_content and cover_filename:
            cover_path = user_dir / f"episode_{episode_id}_cover.jpg"
            with Image.open(BytesIO(cover_content)) as img:
                # N-14: カバーも同じ画素数上限を確認する
                cw, ch = img.size
                if (cw * ch > MAX_IMAGE_PIXELS
                        or max(cw, ch) > MAX_IMAGE_DIMENSION):
                    raise ValueError(
                        f"カバー画像の画素数が上限を超えています ({cw}x{ch})")
                img = img.convert("RGB")
                img.thumbnail((600, 900), Image.LANCZOS)
                img.save(cover_path, "JPEG", quality=85)

        update_progress(
            job_id, "done", len(saved_pages), len(saved_pages),
            page_count=len(saved_pages),
            scrambled=do_scramble,
            tile_size=tile_size,
        )

    except Exception as e:
        update_progress(job_id, "error", 0, 0, message=str(e))
        logger.error(f"Upload error [{job_id}]: {e}")
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir)

# ---------------------------------------------------------------------------
# 作者：アップロード進捗
# ---------------------------------------------------------------------------

@app.get("/api/author/upload-progress/{job_id}")
async def get_upload_progress(
    job_id:       str,
    current_user: User = Depends(get_current_user)
):
    progress = upload_progress.get(job_id)
    if not progress:
        return {"phase": "done", "current": 0, "total": 0}
    # N-21: 他人のジョブの進捗は見せない。ジョブ ID は推測困難だが、
    # 「存在しない」扱い (404) にして情報自体を出さない。
    # owner_id を持たない古い記録 (再起動前の残り等) は従来どおり返す。
    owner_id = progress.get("owner_id")
    if owner_id is not None and owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    if progress["phase"] in ("done", "error"):
        upload_progress.pop(job_id, None)
    # 所有者 ID は応答に含めない
    return {k: v for k, v in progress.items() if k != "owner_id"}

# ---------------------------------------------------------------------------
# 作者：設定保存
# ---------------------------------------------------------------------------

@app.patch("/api/author/titles/{title_id}/episodes/{episode_id}/settings")
async def update_settings(
    title_id:     int,
    episode_id:   int,
    settings:     EpisodePublishSettings,
    current_user: User = Depends(get_current_user)
):
    # 乙-3: 他人の既存番号との衝突は 409 (DB 同期の前に確認する)
    if title_id < 1 or episode_id < 1:
        raise HTTPException(
            status_code=400,
            detail="作品番号とエピソード番号は1以上で指定してください")
    # 丙-2: 送られてきた tile_size の範囲外は入口で 400
    sent_fields = settings.model_fields_set
    if "tile_size" in sent_fields:
        validate_tile_size(settings.tile_size)

    user_dir = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{title_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    settings_path = user_dir / f"episode_{episode_id}_settings.json"

    # 丙-4: 既存設定は共通ヘルパーで読む。壊れていたら退避して作り直す
    # (作者が保存し直すことで修復できる。壊れた控えは残る)
    try:
        existing = read_settings_json(settings_path) or {}
    except SettingsCorruptError as e:
        logger.error(f"設定保存時に壊れた設定を検知しました: {e}")
        backup_corrupt_settings(settings_path)
        existing = {}

    # N-7: 部分更新の意味づけにする。送られてきた項目だけを更新し、
    # 送られなかった項目は既存値を保つ。編集モーダルが送らない
    # rating・タグ類・feedback や、固定値で送られがちな scrambled /
    # tile_size が、保存のたびに消えたり書き換わったりしないようにする。
    _updatable_fields = (
        "status", "access_level", "price", "title_name", "episode_name",
        "caption", "comment", "note", "feedback", "scrambled", "tile_size",
        "webhook_enabled", "rating", "warnings", "genre_tags", "motif_tags",
    )
    merged: dict = {}
    for _field in _updatable_fields:
        if _field in sent_fields:
            merged[_field] = getattr(settings, _field)
        elif _field in existing:
            merged[_field] = existing[_field]
        else:
            merged[_field] = getattr(settings, _field)

    # N-2 (サーバー側): genre_tags は固定表 GENRE_TAGS にある値だけに絞る。
    # settings.json には送られた文字列がそのまま保存され、カタログ経由で
    # 全訪問者の画面に届くため、表に無い文字列 (HTML/JS を含む) を
    # ここで落とす。順序は維持し、重複は取り除く。
    _allowed_genres = set(GENRE_TAGS)
    filtered_genre_tags: List[str] = []
    for _t in merged["genre_tags"]:
        if _t in _allowed_genres and _t not in filtered_genre_tags:
            filtered_genre_tags.append(_t)
    merged["genre_tags"] = filtered_genre_tags

    effective = settings.model_copy(update=merged)

    existing.update(merged)
    existing.update({
        "author_name":     current_user.username,
        "author_id":       current_user.id,
        "title_id":        title_id,
        "episode_id":      episode_id,
        "updated_at":      time.time(),
    })
    if "created_at" not in existing:
        existing["created_at"] = time.time()

    db = SessionLocal()
    try:
        assert_title_episode_available(db, current_user, title_id, episode_id)
        sync_episode_tags(db, current_user, title_id, episode_id, effective)
        ep = db.query(Episode).filter_by(id=episode_id, work_id=title_id).first()
        if ep:
            existing["public_id"] = ep.public_id
    finally:
        db.close()

    # 丙-4: 書き込みは一時ファイル経由の原子的な置き換え
    write_settings_json(settings_path, existing)

    return {"status": "success"}

# ---------------------------------------------------------------------------
# 作者：エピソード削除
# ---------------------------------------------------------------------------

@app.delete("/api/author/episodes/{public_id}")
async def delete_episode(
    public_id:    str,
    current_user: User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(404, "エピソードが見つかりません")
    work = db.query(Work).filter_by(id=ep.work_id, user_id=current_user.id).first()
    if not work:
        raise HTTPException(403, "権限がありません")

    user_dir      = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{work.id}"
    cbz_path      = user_dir / f"episode_{ep.id}.cbz"
    settings_path = user_dir / f"episode_{ep.id}_settings.json"
    cover_path    = user_dir / f"episode_{ep.id}_cover.jpg"
    thumb_dir     = user_dir / f"episode_{ep.id}_thumbs"

    deleted = []
    for path in [cbz_path, settings_path, cover_path]:
        if path.exists():
            path.unlink()
            deleted.append(path.name)
    if thumb_dir.exists():
        shutil.rmtree(thumb_dir)
        deleted.append("thumbs/")

    db.query(EpisodeTag).filter_by(episode_id=ep.id).delete()
    db.query(EpisodeWarning).filter_by(episode_id=ep.id).delete()
    db.query(Reaction).filter_by(episode_id=ep.id).delete()
    db.delete(ep)
    db.commit()

    if user_dir.exists() and not any(user_dir.iterdir()):
        user_dir.rmdir()

    return {"status": "success", "deleted": deleted}

# ---------------------------------------------------------------------------
# 作者：プレビュー用CBZ
# ---------------------------------------------------------------------------

@app.post("/api/author/episodes/{public_id}/preview-token")
async def issue_preview_token(
    public_id:    str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """N-13: プレビュー専用・短命トークンの発行 (要ログイン＋所有者確認)。

    ログイン JWT そのものを URL に載せるのをやめるための入口。
    get_current_user が停止中アカウント (乙-1) もここで断る。
    """
    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    work = db.query(Work).filter_by(id=ep.work_id, user_id=current_user.id).first()
    if not work:
        raise HTTPException(status_code=403, detail="権限がありません")
    token = _make_preview_token(current_user.id, public_id)
    return {
        "preview_token": token,
        "expires_in":    PREVIEW_TOKEN_TTL_MINUTES * 60,
        "preview_url":   f"/api/author/preview/{public_id}.cbz?token={token}",
    }

@app.get("/api/author/preview/{public_id}.cbz")
async def preview_cbz(
    public_id: str,
    token:     str,
    db:        Session = Depends(get_db),
):
    # N-13: 受け付けるのはプレビュー専用トークンだけ (ログイン JWT は不可)。
    # 丙-10: 数字でない sub は _verify_preview_token が 401 にする。
    user_id = _verify_preview_token(token, public_id)

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")

    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    work = db.query(Work).filter_by(id=ep.work_id, user_id=user_id).first()
    if not work:
        raise HTTPException(status_code=403, detail="権限がありません")

    cbz_path = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{work.id}" / f"episode_{ep.id}.cbz"
    if not cbz_path.exists():
        raise HTTPException(status_code=404, detail="CBZファイルが見つかりません")

    return FileResponse(
        path=str(cbz_path),
        media_type="application/zip",
        filename=f"preview_{public_id}.cbz",
        headers={"Cache-Control": "no-store"},
    )

# ---------------------------------------------------------------------------
# 内部：プレビュー作品の設定取得 (N-4)
# ビューワーが作者プレビューを開くとき、作品設定 (scrambled / tile_size)
# を知るためのサーバー間受け口。認証は内部キー＋プレビュー専用トークン
# (preview_cbz と同じ所有者確認)。下書きは公開カタログに載らないため、
# ビューワーはここでしか設定を取得できない。
# ---------------------------------------------------------------------------

@app.get("/api/internal/preview-settings")
async def internal_preview_settings(
    public_id: str,
    token:     str,
    request:   Request,
    db:        Session = Depends(get_db),
):
    provided_key = request.headers.get("x-internal-key") or ""
    if not secrets.compare_digest(provided_key, INTERNAL_API_KEY):
        raise HTTPException(status_code=403, detail="内部キーが不正です")
    # 丙-10: 数字でない sub 等は _verify_preview_token が 401 にする
    user_id = _verify_preview_token(token, public_id)

    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    work = db.query(Work).filter_by(id=ep.work_id, user_id=user_id).first()
    if not work:
        raise HTTPException(status_code=403, detail="権限がありません")

    settings_path = (FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{work.id}"
                     / f"episode_{ep.id}_settings.json")
    meta = read_settings_for_listing(settings_path)
    if meta is None:
        # 設定がまだ無い作品は、アップロード時の既定
        # (アップロードは常にスクランブル済み・タイル16で作る) に合わせる
        return {"scrambled": True, "tile_size": TILE_SIZE_DEFAULT}
    return {
        "scrambled": bool(meta.get("scrambled", True)),
        # 丙-2: 配信側の値も入口と同じ範囲に正規化してから返す
        "tile_size": normalize_tile_size(meta.get("tile_size", TILE_SIZE_DEFAULT)),
    }

# ---------------------------------------------------------------------------
# 作者：サムネイル配信
# ---------------------------------------------------------------------------

@app.get("/api/author/thumb/{public_id}/{index}")
async def author_thumb(
    public_id: str,
    index:     int,
    token:     str,
    db:        Session = Depends(get_db),
):
    # N-13: サムネイルもプレビュー専用トークンだけを受け付ける
    user_id = _verify_preview_token(token, public_id)

    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    work = db.query(Work).filter_by(id=ep.work_id, user_id=user_id).first()
    if not work:
        raise HTTPException(status_code=403, detail="権限がありません")

    thumb_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{work.id}"
        / f"episode_{ep.id}_thumbs" / f"thumb_{index:04d}.jpg"
    )
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="サムネイルが見つかりません")

    return FileResponse(str(thumb_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 内部：CBZトークン発行（ビューワーからのみ呼ばれる）
# ---------------------------------------------------------------------------

@app.get("/api/internal/issue-cbz-token")
async def issue_cbz_token(
    request:        Request,
    public_id:      str = Query(...),
    x_internal_key: Optional[str] = Header(default=None),
    db:             Session = Depends(get_db),
):
    # 丙-7: 秘密の比較はタイミング差を漏らさない compare_digest で行う
    if (not INTERNAL_API_KEY or not x_internal_key
            or not hmac_lib.compare_digest(
                x_internal_key.encode(), INTERNAL_API_KEY.encode())):
        raise HTTPException(status_code=403, detail="内部アクセスのみ許可")
    ep, user_dir, settings_path = resolve_episode_by_public_id(public_id, db)
    # 乙-5 / N-6: トークン発行の時点でも公開可否を確認する
    # (配信受け口側の確認と合わせて二重構えにする)
    meta = require_public_delivery(ep, settings_path)
    return {
        "cbz_token": _make_cbz_token(public_id),
        # 丙-2: 保存済みの値が範囲外でも配信側が落ちないよう正規化する
        "tile_size": normalize_tile_size(meta.get("tile_size",
                                                  TILE_SIZE_DEFAULT)),
        "scrambled": meta.get("scrambled", True),
    }

# ---------------------------------------------------------------------------
# 公開：CBZ配信
# ---------------------------------------------------------------------------

@app.get("/api/public/cbz/{public_id}.cbz")
@limiter.limit("10/minute")
async def serve_public_cbz(
    request:        Request,
    public_id:      str,
    cbz_token:      Optional[str] = Query(default=None),
    referer:        Optional[str] = Header(default=None),
    db:             Session = Depends(get_db),
):
    client_ip = get_client_ip(request)

    # 丙-6: 検証ログにトークン自体は残さない (伏せ方は _mask_token)
    logger.info(f"[DEBUG CBZ] referer={referer!r}")
    logger.info(f"[DEBUG CBZ] viewer_base={get_viewer_base_url()!r}")
    logger.info(f"[DEBUG CBZ] client_ip={client_ip!r}")
    logger.info(f"[DEBUG CBZ] cbz_token={_mask_token(cbz_token)}")

    # Referer検証（ビューワーからのアクセスのみ許可）
    # 丙-6: 前方一致ではなく scheme+host+port の完全一致で判定する
    if not _referer_is_viewer(referer, get_viewer_base_url()):
        logger.info("[DEBUG CBZ] NG: Referer不一致")
        raise HTTPException(status_code=403, detail="不正なアクセス元です")

    # CBZトークン多重検証（HMAC・exp・public_id・nonce使い捨て）
    if not cbz_token or not _verify_cbz_token(cbz_token, public_id):
        logger.info(
            f"[DEBUG CBZ] NG: トークン検証失敗 cbz_token={_mask_token(cbz_token)}")
        raise HTTPException(status_code=403, detail="CBZトークンが無効または期限切れです")

    ep, user_dir, settings_path = resolve_episode_by_public_id(public_id, db)
    # 乙-5 / N-6: 公開可否は共通部品で確認する
    # (下書き・公開範囲が public でない作品は渡さない)
    require_public_delivery(ep, settings_path)
    cbz_path = user_dir / f"episode_{ep.id}.cbz"
    if not cbz_path.exists():
        raise HTTPException(status_code=404, detail="CBZファイルが見つかりません")
    increment_view_count(settings_path, client_ip, episode_id=ep.id)
    return FileResponse(
        path=str(cbz_path),
        media_type="application/zip",
        filename=f"{public_id}.cbz",
        headers={"Cache-Control": "private, no-store"},
    )

# ---------------------------------------------------------------------------
# 公開：サムネイル配信
# ---------------------------------------------------------------------------

@app.get("/api/public/thumb/{public_id}/{index}")
async def public_thumb(public_id: str, index: int, db: Session = Depends(get_db)):
    ep, user_dir, settings_path = resolve_episode_by_public_id(public_id, db)
    # N-6: 設定ファイルが無い・壊れている場合は、公開扱いに倒れず
    # 配信しない (fail-closed)。公開可否は共通部品で確認する
    require_public_delivery(ep, settings_path)
    thumb_path = user_dir / f"episode_{ep.id}_thumbs" / f"thumb_{index:04d}.jpg"
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="サムネイルが見つかりません")
    return FileResponse(str(thumb_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 公開：カタログ
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 丙-5: カタログのメモ化。
# 以前は要求のたびに全 settings.json を読み直し、エピソードごとに
# DB を1件ずつ引いていた。作品数に比例して重くなるため、結果を
# メモリに保持し、「元データの指紋」が変わったときだけ作り直す。
# 指紋に使うのは次の3つで、どれかが変われば必ず作り直しになる:
#   1. settings.json 群の (相対パス, 更新時刻, サイズ) の一覧
#      → 設定保存・削除・公開切替・壊れた設定の退避は全部ここに出る
#   2. episodes テーブルの (件数, view_count 合計)
#      → 閲覧数の増加や作品の追加・削除を拾う集約クエリ1回
#   3. base_url (cbz_url / cover_url の組み立てに使うため)
# 指紋の計算自体は「ファイル一覧の stat」と「集約クエリ1回」だけで、
# 全文読み＋件数分の DB 引きに比べれば十分に軽い。
# ---------------------------------------------------------------------------
_catalog_cache: dict = {"key": None, "value": None}


def _catalog_fingerprint(db: Session):
    files_key = []
    for p in FINAL_ZIP_DIR.glob("user_*/title_*/episode_*_settings.json"):
        try:
            st = p.stat()
            files_key.append((str(p.relative_to(FINAL_ZIP_DIR)),
                              st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    files_key.sort()
    row = db.query(func.count(Episode.id),
                   func.coalesce(func.sum(Episode.view_count), 0)).one()
    return (tuple(files_key), int(row[0]), int(row[1]))


def _build_catalog(base_url: str, db: Session) -> list:
    published_list = []
    for settings_path in FINAL_ZIP_DIR.glob("user_*/title_*/episode_*_settings.json"):
        # 丙-4: 壊れた設定は退避して表に出す (read_settings_for_listing 内)
        meta = read_settings_for_listing(settings_path)
        if meta is None:
            continue
        try:
            if meta.get("status") != "published":
                continue
            ep = db.query(Episode).filter_by(
                id=meta["episode_id"], work_id=meta["title_id"]
            ).first()
            if not ep or not ep.public_id:
                continue
            pid          = ep.public_id
            cover_exists = (settings_path.parent / f"episode_{ep.id}_cover.jpg").exists()
            published_list.append({
                "public_id":       pid,
                # N-19: 画面が代用表示に使う番号も応答に含める
                "title_id":        meta.get("title_id"),
                "episode_id":      meta.get("episode_id"),
                "title_name":      meta.get("title_name",  ""),
                "episode_name":    meta.get("episode_name", ""),
                "author_name":     meta.get("author_name",  ""),
                "caption":         meta.get("caption",      ""),
                "comment":         meta.get("comment",      ""),
                "access_level":    meta.get("access_level", "public"),
                "scrambled":       meta.get("scrambled",    False),
                "tile_size":       meta.get("tile_size",    TILE_SIZE_DEFAULT),
                "webhook_enabled": meta.get("webhook_enabled", False),
                "rating":          meta.get("rating",       "General"),
                "warnings":        meta.get("warnings",     []),
                "genre_tags":      meta.get("genre_tags",   []),
                "motif_tags":      meta.get("motif_tags",   []),
                # N-5: 正本は DB。JSON 側の値ではなく Episode.view_count を返す
                "view_count":      ep.view_count or 0,
                "created_at":      meta.get("created_at",   0),
                "cbz_url":   f"{base_url}/api/public/cbz/{pid}.cbz",
                "cover_url": f"{base_url}/api/public/cover/{pid}" if cover_exists else None,
            })
        except Exception:
            continue
    return published_list


@app.get("/api/public/catalog")
@limiter.limit("60/minute")
async def public_catalog(request: Request, db: Session = Depends(get_db)):
    base_url = str(request.base_url).rstrip("/")
    key = (base_url, _catalog_fingerprint(db))
    if _catalog_cache["key"] == key:
        return _catalog_cache["value"]
    value = _build_catalog(base_url, db)
    _catalog_cache["key"] = key
    _catalog_cache["value"] = value
    return value

# ---------------------------------------------------------------------------
# 公開：リアクション
# ---------------------------------------------------------------------------

class ReactionRequest(BaseModel):
    public_id:     str
    reaction_type: str
    client_ip:     str = ""

VALID_REACTIONS = {"いいね", "しんどい", "ひりひり", "ちかい", "わからない"}

@app.post("/api/public/reaction")
@limiter.limit("20/minute")
async def post_reaction(request: Request, payload: ReactionRequest, db: Session = Depends(get_db)):
    if payload.reaction_type not in VALID_REACTIONS:
        raise HTTPException(status_code=400, detail="無効なリアクション")
    # 乙-2: 本文の client_ip は信用しない (欄自体は互換のため残すが無視する)。
    # 使うのはサーバー側で求めた IP だけ。
    client_ip = get_client_ip(request)
    ep = db.query(Episode).filter_by(public_id=payload.public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    # 乙-2 (監査 R-11 の付け足し): 下書きなど未公開の話には投稿できない
    if ep.status != "published":
        raise HTTPException(status_code=403, detail="この作品は公開されていません")
    # 二重投票の防止: ユニーク制約 (episode_id, reaction_type, client_ip) が
    # DB にもあるが、先に存在確認して added=False をきれいに返す
    existing = db.query(Reaction).filter_by(
        episode_id=ep.id, reaction_type=payload.reaction_type,
        client_ip=client_ip).first()
    if existing:
        added = False
    else:
        try:
            db.add(Reaction(episode_id=ep.id, reaction_type=payload.reaction_type, client_ip=client_ip))
            db.commit()
            added = True
        except Exception:
            db.rollback()
            added = False
    counts = {rt: db.query(Reaction).filter_by(episode_id=ep.id, reaction_type=rt).count()
              for rt in VALID_REACTIONS}
    return {"added": added, "counts": counts}

@app.get("/api/public/reactions/{public_id}")
async def get_reactions(public_id: str, db: Session = Depends(get_db)):
    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        return {"public_id": public_id, "counts": {}}
    counts = {rt: db.query(Reaction).filter_by(episode_id=ep.id, reaction_type=rt).count()
              for rt in VALID_REACTIONS}
    return {"public_id": public_id, "counts": counts}

# ---------------------------------------------------------------------------
# 公開：カバー画像配信
# ---------------------------------------------------------------------------

@app.get("/api/public/cover/{public_id}")
async def public_cover(public_id: str, db: Session = Depends(get_db)):
    ep, user_dir, settings_path = resolve_episode_by_public_id(public_id, db)
    # 乙-6 / 乙-5: 公開中 (published かつ access_level=public) の話だけを
    # 未認証で返す。下書きや限定公開のカバーは作者用受け口を使う
    require_public_delivery(ep, settings_path)
    cover_path = user_dir / f"episode_{ep.id}_cover.jpg"
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="カバー画像が見つかりません")
    return FileResponse(str(cover_path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

# ---------------------------------------------------------------------------
# 作者：カバー画像配信 (自分の作品専用)
# ---------------------------------------------------------------------------

@app.get("/api/author/cover/{public_id}")
async def author_cover(
    public_id: str,
    token:     str = "",
    db:        Session = Depends(get_db),
):
    """乙-6: 作者本人が自分のカバーを確認するための受け口。

    公開受け口は下書き・限定公開のカバーを返さなくなったため、作者画面の
    作品一覧はこちらを使う。<img> では Authorization ヘッダを付けられない
    ので、鍵はプレビュー専用トークン (N-13 で新設した、短命・作品限定の
    もの) をクエリで受ける。作品の所有者であることも確認する。
    """
    user_id = _verify_preview_token(token, public_id)

    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    work = db.query(Work).filter_by(id=ep.work_id, user_id=user_id).first()
    if not work:
        raise HTTPException(status_code=403, detail="権限がありません")

    cover_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{work.id}"
        / f"episode_{ep.id}_cover.jpg"
    )
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="カバー画像が見つかりません")
    return FileResponse(str(cover_path), media_type="image/jpeg",
                        headers={"Cache-Control": "private, no-store"})

# ---------------------------------------------------------------------------
# 設定情報
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {"platform_base_url": base_url}

# ---------------------------------------------------------------------------
# アドミン認証
# ---------------------------------------------------------------------------
# 甲-2 (案ア): 管理者トークンの署名鍵も環境変数から読む。既定値は置かない。
# 管理者を使わない通常運用まで起動不能にしないため、未設定そのものは
# ここでは許すが、その場合は管理者機能が一切動かない形にする。
# ただし ADMIN_PASSWORD だけ設定して鍵を忘れた場合は、既定鍵で
# 動いてしまう事故を防ぐため、起動時に落とす。
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "")
ADMIN_PASSWORD   = os.environ.get("ADMIN_PASSWORD",   "")
if ADMIN_PASSWORD and not ADMIN_SECRET_KEY:
    raise RuntimeError(
        "環境変数 ADMIN_PASSWORD が設定されていますが ADMIN_SECRET_KEY が"
        "設定されていません。管理者トークンの署名鍵を、環境変数 "
        "ADMIN_SECRET_KEY に十分に長いランダムな文字列で設定してください。"
    )

class AdminTokenRequest(BaseModel):
    password: str

def _verify_admin_token(token: str) -> bool:
    if not ADMIN_SECRET_KEY:
        return False
    try:
        payload = jwt.decode(token, ADMIN_SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("role") == "admin"
    except JWTError:
        return False

def get_admin_user(token: str = Depends(OAuth2PasswordBearer(tokenUrl="/api/admin/token"))):
    if not _verify_admin_token(token):
        raise HTTPException(status_code=403, detail="管理者権限が必要です")

@app.post("/api/admin/token")
@limiter.limit("5/minute")
async def admin_login(request: Request, body: AdminTokenRequest):
    if not ADMIN_PASSWORD or not ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="管理機能は無効です")
    # 甲-2 / 丙-7: パスワード比較もタイミング差を漏らさない形にする
    if not hmac_lib.compare_digest(
            body.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(status_code=401, detail="パスワードが正しくありません")
    token = jwt.encode(
        {"role": "admin", "exp": datetime.now(UTC) + timedelta(hours=8)},
        ADMIN_SECRET_KEY, algorithm=ALGORITHM
    )
    return {"access_token": token, "token_type": "bearer"}

@app.get("/api/admin/users")
async def admin_list_users(
    db: Session = Depends(get_db),
    _:  None    = Depends(get_admin_user),
):
    users = db.query(User).order_by(User.id).all()
    return [
        {"id": u.id, "username": u.username, "email": u.email,
         "is_active": u.is_active, "work_count": len(u.works)}
        for u in users
    ]

class AdminActiveUpdate(BaseModel):
    is_active: bool

@app.patch("/api/admin/users/{user_id}/active")
async def admin_toggle_user(
    user_id: int,
    body:    AdminActiveUpdate,
    db:      Session = Depends(get_db),
    _:       None    = Depends(get_admin_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    user.is_active = body.is_active
    if not body.is_active:
        work_ids = [w.id for w in user.works]
        if work_ids:
            affected = (
                db.query(Episode)
                .filter(Episode.work_id.in_(work_ids), Episode.status == "published")
                .all()
            )
            for ep in affected:
                ep.status = "draft"
                settings_path = (
                    FINAL_ZIP_DIR / f"user_{user.id}" / f"title_{ep.work_id}"
                    / f"episode_{ep.id}_settings.json"
                )
                if settings_path.exists():
                    try:
                        # 丙-4: 読み書きは共通ヘルパー (原子的な置き換え)
                        meta = read_settings_json(settings_path)
                        if meta is not None:
                            meta["status"] = "draft"
                            write_settings_json(settings_path, meta)
                    except Exception as e:
                        logger.warning(f"settings.json status更新失敗 ep={ep.id}: {e}")
            unpublished_count = len(affected)
        else:
            unpublished_count = 0
    else:
        unpublished_count = 0
    db.commit()
    return {"id": user.id, "is_active": user.is_active, "unpublished_count": unpublished_count}

@app.get("/api/admin/episodes")
async def admin_list_episodes(
    db: Session = Depends(get_db),
    _:  None    = Depends(get_admin_user),
):
    episodes = (
        db.query(Episode)
        .options(joinedload(Episode.work).joinedload(Work.author))
        .order_by(Episode.id.desc())
        .all()
    )
    result = []
    for ep in episodes:
        work = ep.work
        settings_path = (
            FINAL_ZIP_DIR
            / f"user_{work.user_id if work else 0}"
            / f"title_{ep.work_id}"
            / f"episode_{ep.id}_settings.json"
        )
        feedback_from_file = ""
        if settings_path.exists():
            try:
                meta = read_settings_for_listing(settings_path)
                if meta is not None:
                    feedback_from_file = meta.get("feedback", "")
            except Exception:
                pass
        result.append({
            "id":         ep.id,
            "public_id":  ep.public_id,
            "title":      ep.title,
            "status":     ep.status,
            "rating":     ep.rating,
            "view_count": ep.view_count,
            "feedback":   ep.feedback or feedback_from_file,
            "work_id":    ep.work_id,
            "author":     work.author.username if work and work.author else "",
            "created_at": ep.created_at.isoformat() if ep.created_at else "",
        })
    return result

@app.delete("/api/admin/episodes/{public_id}")
async def admin_delete_episode(
    public_id: str,
    db:        Session = Depends(get_db),
    _:         None    = Depends(get_admin_user),
):
    ep = db.query(Episode).filter_by(public_id=public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
    work    = db.query(Work).filter(Work.id == ep.work_id).first()
    user_id = work.user_id if work else 0

    user_dir      = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{ep.work_id}"
    cbz_path      = user_dir / f"episode_{ep.id}.cbz"
    settings_path = user_dir / f"episode_{ep.id}_settings.json"
    cover_path    = user_dir / f"episode_{ep.id}_cover.jpg"
    thumb_dir     = user_dir / f"episode_{ep.id}_thumbs"

    deleted = []
    for path in [cbz_path, settings_path, cover_path]:
        if path.exists():
            path.unlink()
            deleted.append(path.name)
    if thumb_dir.exists():
        shutil.rmtree(thumb_dir)
        deleted.append("thumbs/")

    db.query(EpisodeTag).filter_by(episode_id=ep.id).delete()
    db.query(EpisodeWarning).filter_by(episode_id=ep.id).delete()
    db.query(Reaction).filter_by(episode_id=ep.id).delete()
    db.delete(ep)
    db.commit()
    return {"status": "deleted", "deleted_files": deleted}

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="管理機能は無効です")
    admin_html_path = STATIC_DIR / "admin.html"
    if not admin_html_path.exists():
        raise HTTPException(status_code=404, detail="admin.html が見つかりません")
    with open(admin_html_path, "r", encoding="utf-8") as f:
        return f.read()

# ---------------------------------------------------------------------------
# 画面配信
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open(STATIC_DIR / "index.html", "r", encoding="utf-8") as f:
        return f.read()