import os
import re
import uuid
import json
import time
import shutil
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
from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime, UniqueConstraint, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship, joinedload
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
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
SECRET_KEY                  = "MANGA_PLATFORM_SUPER_SECRET_KEY"
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
CHARSET_62                   = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

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
    if time.time() > payload["exp"]:
        return False
    if payload.get("pid") != public_id:
        return False
    if not _consume_nonce(payload.get("nonce", "")):
        return False
    return True

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
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
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

    db.commit()

# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    return cf_ip if cf_ip else (request.client.host if request.client else "unknown")

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

def increment_view_count(settings_path: Path, client_ip: str) -> None:
    cooldown_key = f"{settings_path}:{client_ip}"
    now = time.time()
    if now - _view_cooldown.get(cooldown_key, 0) < VIEW_COOLDOWN_SECONDS:
        return
    try:
        with open(settings_path, "r") as f:
            meta = json.load(f)
        meta["view_count"] = meta.get("view_count", 0) + 1
        with open(settings_path, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _view_cooldown[cooldown_key] = now
    except Exception as e:
        logger.warning(f"view_count更新失敗: {e}")

def update_progress(job_id: str, phase: str, current: int, total: int, **kwargs):
    upload_progress[job_id] = {"phase": phase, "current": current, "total": total, **kwargs}

# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------
def get_ip_for_limit(request: Request) -> str:
    return get_client_ip(request)

limiter = Limiter(key_func=get_ip_for_limit)

# ---------------------------------------------------------------------------
# リクエストボディサイズ制限ミドルウェア
# ---------------------------------------------------------------------------
class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "upload-and-zip" in str(request.url.path):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_TOTAL_SIZE_BYTES + 5 * 1024 * 1024:
                return Response("リクエストサイズが上限を超えています", status_code=413)
        return await call_next(request)

# ---------------------------------------------------------------------------
# FastAPI アプリ
# ---------------------------------------------------------------------------
app = FastAPI()
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(LimitUploadSizeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

@app.post("/api/auth/signup")
@limiter.limit("3/hour")
async def signup(request: Request, user_data: UserRegister, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(400, "登録済みのメールアドレスです")
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(400, "使用済みのユーザー名です")
    hashed_pw = pwd_context.hash(user_data.password)
    db.add(User(username=user_data.username, email=user_data.email, hashed_password=hashed_pw))
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
        try:
            with open(settings_path, "r") as f:
                meta = json.load(f)
            cbz_path   = settings_path.parent / f"episode_{meta['episode_id']}.cbz"
            cover_path = settings_path.parent / f"episode_{meta['episode_id']}_cover.jpg"
            meta["has_cbz"]   = cbz_path.exists()
            meta["has_cover"] = cover_path.exists()
            ep = db.query(Episode).filter_by(
                id=meta["episode_id"], work_id=meta["title_id"]
            ).first()
            meta["public_id"] = ep.public_id if ep else None
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
):
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
    update_progress(job_id, "queued", 0, len(files))

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
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue

            update_progress(job_id, "resize", index + 1, total)

            raw_path = work_dir / f"raw_{filename}"
            with open(raw_path, "wb") as f:
                f.write(content)

            with Image.open(raw_path) as img:
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
    if progress["phase"] in ("done", "error"):
        upload_progress.pop(job_id, None)
    return progress

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
    user_dir = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{title_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    settings_path = user_dir / f"episode_{episode_id}_settings.json"

    existing = {}
    if settings_path.exists():
        with open(settings_path, "r") as f:
            existing = json.load(f)

    existing.update({
        "status":          settings.status,
        "access_level":    settings.access_level,
        "price":           settings.price,
        "title_name":      settings.title_name,
        "episode_name":    settings.episode_name,
        "caption":         settings.caption,
        "comment":         settings.comment,
        "note":            settings.note,
        "feedback":        settings.feedback,
        "scrambled":       settings.scrambled,
        "tile_size":       settings.tile_size,
        "webhook_enabled": settings.webhook_enabled,
        "author_name":     current_user.username,
        "author_id":       current_user.id,
        "title_id":        title_id,
        "episode_id":      episode_id,
        "updated_at":      time.time(),
        "rating":          settings.rating,
        "warnings":        settings.warnings,
        "genre_tags":      settings.genre_tags,
        "motif_tags":      settings.motif_tags,
    })
    if "created_at" not in existing:
        existing["created_at"] = time.time()

    db = SessionLocal()
    try:
        sync_episode_tags(db, current_user, title_id, episode_id, settings)
        ep = db.query(Episode).filter_by(id=episode_id, work_id=title_id).first()
        if ep:
            existing["public_id"] = ep.public_id
    finally:
        db.close()

    with open(settings_path, "w") as f:
        json.dump(existing, f, indent=4, ensure_ascii=False)

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

@app.get("/api/author/preview/{public_id}.cbz")
async def preview_cbz(
    public_id: str,
    token:     str,
    db:        Session = Depends(get_db),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except JWTError:
        raise HTTPException(status_code=401, detail="トークンが無効または期限切れです")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
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
# 作者：サムネイル配信
# ---------------------------------------------------------------------------

@app.get("/api/author/thumb/{public_id}/{index}")
async def author_thumb(
    public_id: str,
    index:     int,
    token:     str,
    db:        Session = Depends(get_db),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except JWTError:
        raise HTTPException(status_code=401, detail="トークンが無効です")

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
    if not INTERNAL_API_KEY or x_internal_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="内部アクセスのみ許可")
    ep, user_dir, settings_path = resolve_episode_by_public_id(public_id, db)
    tile_size = TILE_SIZE_DEFAULT
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                meta = json.load(f)
            tile_size = meta.get("tile_size", TILE_SIZE_DEFAULT)
        except Exception:
            pass
    return {
        "cbz_token": _make_cbz_token(public_id),
        "tile_size": tile_size,
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

    # [DEBUG] Referer検証ログ
    logger.info(f"[DEBUG CBZ] referer={referer!r}")
    logger.info(f"[DEBUG CBZ] viewer_base={get_viewer_base_url()!r}")
    logger.info(f"[DEBUG CBZ] client_ip={client_ip!r}")

    # Referer検証（ビューワーからのアクセスのみ許可）
    if not referer or not referer.startswith(get_viewer_base_url()):
        logger.info("[DEBUG CBZ] NG: Referer不一致")
        raise HTTPException(status_code=403, detail="不正なアクセス元です")

    # CBZトークン多重検証（HMAC・exp・public_id・nonce使い捨て）
    if not cbz_token or not _verify_cbz_token(cbz_token, public_id):
        logger.info(f"[DEBUG CBZ] NG: トークン検証失敗 cbz_token={cbz_token!r}")
        raise HTTPException(status_code=403, detail="CBZトークンが無効または期限切れです")

    ep, user_dir, settings_path = resolve_episode_by_public_id(public_id, db)
    if not settings_path.exists():
        raise HTTPException(status_code=404, detail="設定ファイルが見つかりません")
    with open(settings_path, "r") as f:
        meta = json.load(f)
    if meta.get("status") != "published":
        raise HTTPException(status_code=403, detail="この作品は非公開です")
    cbz_path = user_dir / f"episode_{ep.id}.cbz"
    if not cbz_path.exists():
        raise HTTPException(status_code=404, detail="CBZファイルが見つかりません")
    increment_view_count(settings_path, client_ip)
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
    if settings_path.exists():
        with open(settings_path, "r") as f:
            meta = json.load(f)
        if meta.get("status") != "published":
            raise HTTPException(status_code=403, detail="非公開です")
    thumb_path = user_dir / f"episode_{ep.id}_thumbs" / f"thumb_{index:04d}.jpg"
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="サムネイルが見つかりません")
    return FileResponse(str(thumb_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 公開：カタログ
# ---------------------------------------------------------------------------

@app.get("/api/public/catalog")
@limiter.limit("60/minute")
async def public_catalog(request: Request, db: Session = Depends(get_db)):
    base_url       = str(request.base_url).rstrip("/")
    published_list = []
    for settings_path in FINAL_ZIP_DIR.glob("user_*/title_*/episode_*_settings.json"):
        try:
            with open(settings_path, "r") as f:
                meta = json.load(f)
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
                "view_count":      meta.get("view_count",   0),
                "created_at":      meta.get("created_at",   0),
                "cbz_url":   f"{base_url}/api/public/cbz/{pid}.cbz",
                "cover_url": f"{base_url}/api/public/cover/{pid}" if cover_exists else None,
            })
        except Exception:
            continue
    return published_list

# ---------------------------------------------------------------------------
# 公開：リアクション
# ---------------------------------------------------------------------------

class ReactionRequest(BaseModel):
    public_id:     str
    reaction_type: str
    client_ip:     str = ""

VALID_REACTIONS = {"いいね", "しんどい", "ひりひり", "ちかい", "わからない"}

@app.post("/api/public/reaction")
async def post_reaction(payload: ReactionRequest, request: Request, db: Session = Depends(get_db)):
    if payload.reaction_type not in VALID_REACTIONS:
        raise HTTPException(status_code=400, detail="無効なリアクション")
    client_ip = payload.client_ip or get_client_ip(request)
    ep = db.query(Episode).filter_by(public_id=payload.public_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="エピソードが見つかりません")
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
    ep, user_dir, _ = resolve_episode_by_public_id(public_id, db)
    cover_path = user_dir / f"episode_{ep.id}_cover.jpg"
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="カバー画像が見つかりません")
    return FileResponse(str(cover_path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

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
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "ADMIN_SUPER_SECRET_CHANGE_THIS")
ADMIN_PASSWORD   = os.environ.get("ADMIN_PASSWORD",   "")

class AdminTokenRequest(BaseModel):
    password: str

def _verify_admin_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, ADMIN_SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("role") == "admin"
    except JWTError:
        return False

def get_admin_user(token: str = Depends(OAuth2PasswordBearer(tokenUrl="/api/admin/token"))):
    if not _verify_admin_token(token):
        raise HTTPException(status_code=403, detail="管理者権限が必要です")

@app.post("/api/admin/token")
async def admin_login(body: AdminTokenRequest):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="管理機能は無効です")
    if body.password != ADMIN_PASSWORD:
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
                        with open(settings_path, "r") as f:
                            meta = json.load(f)
                        meta["status"] = "draft"
                        with open(settings_path, "w") as f:
                            json.dump(meta, f, ensure_ascii=False, indent=4)
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
                with open(settings_path) as f:
                    meta = json.load(f)
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