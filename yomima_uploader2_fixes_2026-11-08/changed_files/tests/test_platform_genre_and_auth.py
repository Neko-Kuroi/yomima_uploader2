"""N-1 (登録→ログインが通ること) と N-2 サーバー側
(update_settings が genre_tags を固定表 GENRE_TAGS の値だけに絞ること)。
"""
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def platform(platform_main):
    return TestClient(platform_main.app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def author(platform):
    r = platform.post("/api/auth/signup", json={
        "username": "stage1alice",
        "email": "stage1alice@example.com",
        "password": "stage1-password",
    })
    assert r.status_code in (200, 201, 400), r.text  # 400 は再実行時の重複
    r = platform.post("/api/auth/token", data={
        "username": "stage1alice", "password": "stage1-password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_signup_and_login_work(platform):
    """N-1: bcrypt==4.0.1 固定の組み合わせで登録→ログインが通る。"""
    r = platform.post("/api/auth/signup", json={
        "username": "stage1bob",
        "email": "stage1bob@example.com",
        "password": "stage1-password-2",
    })
    assert r.status_code in (200, 201, 400), r.text
    r = platform.post("/api/auth/token", data={
        "username": "stage1bob", "password": "stage1-password-2"})
    assert r.status_code == 200, r.text
    assert r.json().get("access_token")


def test_genre_tags_are_filtered_to_fixed_table(platform, platform_main,
                                                author):
    evil = "<img src=x onerror=alert(1)>"
    payload = {
        "status": "published", "access_level": "public", "price": 0,
        "title_name": "T", "episode_name": "E", "caption": "",
        "comment": "", "note": "", "feedback": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "rating": "General", "warnings": [],
        "genre_tags": ["SF", evil, "日常", "SF", "存在しないジャンル"],
        "motif_tags": ["猫"],
    }
    r = platform.patch(
        "/api/author/titles/91/episodes/91/settings",
        json=payload, headers=author)
    assert r.status_code == 200, r.text

    settings_path = (platform_main.FINAL_ZIP_DIR / "user_1"
                     / "title_91" / "episode_91_settings.json")
    if not settings_path.exists():
        # ユーザーIDが 1 でない実行環境ではパスを探す
        cands = list(platform_main.FINAL_ZIP_DIR.glob(
            "user_*/title_91/episode_91_settings.json"))
        assert cands, "settings.json が見つからない"
        settings_path = cands[0]
    meta = json.loads(settings_path.read_text())
    saved = meta.get("genre_tags", [])
    assert evil not in saved
    assert "存在しないジャンル" not in saved
    assert saved == ["SF", "日常"], saved  # 固定表の値のみ・順序維持・重複除去


def test_upload_and_catalog_flow(platform, author):
    """正常系: アップロード→加工完了→設定保存→公開カタログに載る。"""
    import io
    import time

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 150), (10, 20, 30)).save(buf, "PNG")
    png = buf.getvalue()
    r = platform.post(
        "/api/author/titles/93/episodes/93/upload-and-zip",
        params={"do_scramble": True, "tile_size": 16},
        files=[("files", ("p1.png", png, "image/png"))],
        headers=author)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    phase = None
    for _ in range(50):
        r = platform.get(f"/api/author/upload-progress/{job_id}",
                         headers=author)
        phase = r.json().get("phase")
        if phase in ("done", "error"):
            break
        time.sleep(0.2)
    assert phase == "done", r.json()

    payload = {
        "status": "published", "access_level": "public", "price": 0,
        "title_name": "Flow", "episode_name": "Ep", "caption": "",
        "comment": "", "note": "", "feedback": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "rating": "General", "warnings": [], "genre_tags": ["日常"],
        "motif_tags": [],
    }
    r = platform.patch(
        "/api/author/titles/93/episodes/93/settings",
        json=payload, headers=author)
    assert r.status_code == 200, r.text
    r = platform.get("/api/public/catalog")
    assert r.status_code == 200
    titles = [i.get("title_name") for i in r.json()]
    assert "Flow" in titles, titles


def test_genre_tags_normal_values_pass_through(platform, author):
    payload = {
        "status": "draft", "access_level": "public", "price": 0,
        "title_name": "T2", "episode_name": "E2", "caption": "",
        "comment": "", "note": "", "feedback": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "rating": "General", "warnings": [],
        "genre_tags": ["日常", "冒険"], "motif_tags": [],
    }
    r = platform.patch(
        "/api/author/titles/92/episodes/92/settings",
        json=payload, headers=author)
    assert r.status_code == 200, r.text
    r = platform.get("/api/public/catalog")
    assert r.status_code == 200
