"""N-2 画面側: 危険な onclick 文字列埋め込みが残っていないことの静的確認。

ブラウザ実行までは自動化しない代わりに、「値を JS 文字列へ埋める形」が
ソースから消え、dataset / textContent 方式に置き換わったことを固定する。
手作業の裏取りとして node での構文確認も別途行う (STAGE1_REPORT 参照)。
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, "mangaupload_yomima_site", "static", "index.html")
ADMIN = os.path.join(REPO, "mangaupload_yomima_site", "static", "admin.html")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_genre_filter_buttons_not_built_with_innerhtml_onclick():
    src = _read(INDEX)
    assert "onclick=\"toggleGenreFilter('${g}')\"" not in src
    assert 'onclick="toggleGenreFilter(\'${g}\')"' not in src
    # 置き換え方式: ボタンを DOM で作り dataset + addEventListener
    assert "buildGenreFilterButtons" in src
    assert "dataset" in src


def test_catalog_tag_chips_have_no_onclick_string_embedding():
    src = _read(INDEX)
    assert "onclick=\"clickCatalogTag('${esc(t)}')\"" not in src
    assert "catalog-tag" in src  # チップ自体は残っている


def test_admin_dialogs_have_no_onclick_string_embedding():
    src = _read(ADMIN)
    assert "'${esc(u.username)}'" not in src
    assert "openDelDialog('${esc(e.public_id)}'" not in src
    assert "openToggleDialog(${u.id}," not in src or "data-" in src


def test_filename_displays_use_textcontent():
    src = _read(INDEX)
    # サムネイル名にファイル名を無加工 innerHTML で入れない
    assert '<div class="thumb-name">${f.name}</div>' not in src
    # カバー選択プレビューのファイル名も同様
    assert "${file.name}</span>" not in src


# ---------------------------------------------------------------------------
# 丁-1 補完: 画面全体の innerHTML 棚卸しで見つけた残りの危険箇所
# (STAGE4_SUPPLEMENT_DING1 参照)
# ---------------------------------------------------------------------------
import glob
import re

TEMPLATES = os.path.join(REPO, "mangaupload_yomima_site", "yomima", "templates")
READER = os.path.join(TEMPLATES, "reader.html")
PUBLIC = os.path.join(TEMPLATES, "public.html")


def test_index_error_messages_are_not_interpolated_into_innerhtml():
    src = _read(INDEX)
    # e.message (サーバー由来の文字列を含み得る) を innerHTML へ入れない
    assert "読み込みエラー: ${e.message}</div>" not in src
    assert "エラー: ${e.message}</div>" not in src
    assert "errDiv.textContent" in src


def test_catalog_read_button_uses_dataset():
    src = _read(INDEX)
    # URL を onclick の JS 文字列へ埋めない (esc は JS 文脈では効かない)
    assert "onclick=\"openViewer('${esc(m.cbz_url)}')\"" not in src
    assert "catalog-read-btn" in src
    assert "data-cbz-url" in src
    assert "btn.dataset.cbzUrl" in src


def test_myworks_buttons_use_dataset_not_embedded_values():
    src = _read(INDEX)
    # 編集: 作品オブジェクトの JSON を onclick 属性へ埋めない
    assert "onclick='openEditModal(" not in src
    # 削除: 作品名 (利用者の入力) を JS 文字列へ埋めない
    assert "onclick=\"openDeleteModal('" not in src
    assert "edit-work-btn" in src
    assert "delete-work-btn" in src
    assert "myWorksCache" in src


def test_reader_discord_button_is_dom_built():
    src = _read(READER)
    # ファイル名の手製エスケープ (' と \ のみ) + onclick 文字列埋め込み廃止
    assert "safeFilename" not in src
    assert "onclick=\"sendDiscord('" not in src
    assert "addEventListener('click', () => sendDiscord(filename))" in src


def test_reader_reaction_counts_use_textcontent():
    src = _read(READER)
    # 件数表示はサーバー応答を innerHTML へ文字列組み立てしない
    assert "el.innerHTML = Object.entries(counts)" not in src
    assert "createElement('span')" in src


def test_public_read_button_uses_dataset():
    src = _read(PUBLIC)
    # Jinja の |e (HTML 用) を JS 文字列文脈に使わない
    assert "openInReader('{{" not in src
    assert "data-cbz-url" in src
    assert "btn.dataset.cbzUrl" in src


def test_no_safe_filter_in_viewer_templates():
    # サーバー側 partial の自動エスケープを無効化する |safe が無いこと
    for path in glob.glob(os.path.join(TEMPLATES, "**", "*.html"), recursive=True):
        src = _read(path)
        assert "|safe" not in src and "| safe" not in src, path


def test_no_onclick_js_string_interpolation_left():
    # onclick 属性へ ${...} 式展開で値を埋める箇所は、index.html の
    # renderGenreTags (同一ファイル内の固定定数 GENRE_TAGS 専用) だけ
    allowed = {"toggleGenreTag('${tag}')"}
    targets = [INDEX, ADMIN, READER, PUBLIC,
               os.path.join(TEMPLATES, "list.html")]
    targets += glob.glob(os.path.join(TEMPLATES, "partials", "*.html"))
    for path in targets:
        src = _read(path)
        for m in re.findall(r'onclick="[^"]*\$\{[^"]*"', src):
            assert any(a in m for a in allowed), (path, m)
        assert not re.search(r"onclick='[^']*\$\{", src), path
        assert not re.search(r'onclick="[^"]*\{\{', src), path
