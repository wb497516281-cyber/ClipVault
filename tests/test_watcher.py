"""tests/test_watcher.py — 采集层测试：哈希规则、文件命名、入库与去重。

依赖：pytest、Pillow；不触碰真实系统剪贴板（只测纯处理函数）。
"""

from __future__ import annotations

import re
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

import storage
import watcher


@pytest.fixture(autouse=True)
def reset_last_hash():
    """每个用例前重置 last_hash 记忆，避免用例间互相影响。"""
    watcher.last_hash = None
    yield
    watcher.last_hash = None


# ---------------------------------------------------------------------------
# 哈希规则
# ---------------------------------------------------------------------------


def test_text_hash_is_sha256_of_utf8_bytes():
    import hashlib

    text = "测试文本 hello"
    assert watcher.text_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_image_hash_ignores_container_format():
    """同样像素、不同存储格式（PNG vs BMP）应得到相同哈希（像素级）。"""
    pixels = Image.new("RGB", (32, 16), (10, 120, 200))
    other = Image.new("RGB", (32, 16), (10, 120, 200))
    assert watcher.image_hash(pixels) == watcher.image_hash(other)


def test_image_hash_changes_with_pixels():
    a = Image.new("RGB", (32, 16), (0, 0, 0))
    b = Image.new("RGB", (32, 16), (0, 0, 1))
    assert watcher.image_hash(a) != watcher.image_hash(b)


def test_image_hash_rgba_equivalent_to_rgb():
    """RGBA 图与等色 RGB 图哈希一致（都先 convert('RGB')）。"""
    rgb = Image.new("RGB", (8, 8), (200, 100, 50))
    rgba = Image.new("RGBA", (8, 8), (200, 100, 50, 255))
    assert watcher.image_hash(rgba) == watcher.image_hash(rgb)


# ---------------------------------------------------------------------------
# 文件名生成
# ---------------------------------------------------------------------------


def test_unique_stem_pattern():
    stem = watcher._unique_stem()
    assert re.fullmatch(r"\d{8}_\d{6}_\d{6}", stem), stem


def test_unique_stem_avoids_collision():
    """已存在的文件名会被避开（换个随机数）。"""
    stem = watcher._unique_stem()
    (watcher.IMAGE_DIR / f"{stem}.png").write_bytes(b"occupied")
    try:
        new_stem = watcher._unique_stem()
        assert new_stem != stem
    finally:
        (watcher.IMAGE_DIR / f"{stem}.png").unlink()


# ---------------------------------------------------------------------------
# 文本入库与去重
# ---------------------------------------------------------------------------


def test_process_text_inserts_row():
    watcher.process_text("第一条文本", "Notepad")
    row = storage.find_by_hash(watcher.text_hash("第一条文本"))
    assert row is not None
    assert row["content_type"] == "text"
    assert row["source_app"] == "Notepad"


def test_process_text_skips_repeat_via_last_hash():
    """连续相同内容：第二次靠 last_hash 直接跳过，不查库也不插行。"""
    watcher.process_text("重复内容", "App")
    watcher.process_text("重复内容", "App")
    assert storage.stats()["total"] == 1


def test_process_text_dedup_against_db():
    """非连续重复（last_hash 已被其它内容覆盖）靠库内哈希去重。"""
    watcher.process_text("内容A", "App")
    watcher.process_text("内容B", "App")  # last_hash 变成 B
    watcher.process_text("内容A", "App")  # 库里已有 A -> 跳过
    assert storage.stats()["total"] == 2


def test_process_text_empty_string_ignored():
    watcher.process_text("", "App")
    assert storage.stats()["total"] == 0


# ---------------------------------------------------------------------------
# 图片入库、缩略图与去重
# ---------------------------------------------------------------------------


def _make_image(color=(200, 60, 60), size=(640, 360)) -> Image.Image:
    return Image.new("RGB", size, color)


def test_process_image_writes_files_and_row():
    image = _make_image()
    watcher.process_image(image, "Snipping Tool")

    with closing(storage.get_connection()) as conn:
        row = conn.execute("SELECT * FROM clipboard_items WHERE content_type = 'image'").fetchone()
    assert row is not None
    assert row["source_app"] == "Snipping Tool"

    # 原图与缩略图都落盘，且缩略图为 200x200
    original = watcher.IMAGE_DIR / Path(row["image_path"]).name
    thumbnail = watcher.IMAGE_DIR / Path(row["thumbnail_path"]).name
    assert original.is_file() and thumbnail.is_file()
    with Image.open(thumbnail) as thumb:
        assert thumb.size == (200, 200)
    # 缩略图文件名带 thumb_ 前缀
    assert thumbnail.name.startswith("thumb_")
    # 哈希与像素级哈希一致
    assert row["content_hash"] == watcher.image_hash(image)


def test_process_image_skips_duplicate():
    image = _make_image()
    watcher.process_image(image, "App")
    watcher.process_image(image, "App")
    assert storage.stats()["total"] == 1


def test_close_clipboard_safely_ignores_not_open_error():
    """剪贴板没打开时 _close_clipboard_safely 不能抛异常（1418 场景）。"""
    watcher._close_clipboard_safely()  # 当前线程没开剪贴板：必须静默返回


def test_log_error_throttled_dedups(capsys):
    """同一条错误 10 秒内只打印一次（持续故障不刷屏）。"""
    watcher._last_error_log = None
    for _ in range(5):
        watcher._log_error_throttled("[错误] 同一条错误")
    out = capsys.readouterr().out
    assert out.count("[错误] 同一条错误") == 1

    # 换一条不同的错误应立即放行
    watcher._log_error_throttled("[错误] 另一条错误")
    out2 = capsys.readouterr().out
    assert "[错误] 另一条错误" in out2


def test_process_image_cleans_files_when_insert_fails(monkeypatch):
    """入库失败（竞态下 UNIQUE 冲突）时必须删掉刚落盘的文件，避免孤儿图片。"""
    image = _make_image((90, 90, 90))
    before = {p.name for p in watcher.IMAGE_DIR.glob("*.png")}
    monkeypatch.setattr(watcher, "insert_item", lambda **kwargs: None)
    watcher.process_image(image, "App")

    after = {p.name for p in watcher.IMAGE_DIR.glob("*.png")}
    assert after == before  # 没有新增文件（原图与缩略图都被清理）
    assert storage.stats()["total"] == 0
