"""tests/test_storage.py — 存储层测试：建表/迁移/插入/去重/路径/向量。

依赖：pytest；无需剪贴板与网络。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import storage

# ---------------------------------------------------------------------------
# 初始化与迁移
# ---------------------------------------------------------------------------


def test_init_db_creates_files_and_tables():
    """init_db 应创建数据库文件、两张表与全部预期列。"""
    storage.init_db()
    assert storage.DB_PATH.is_file()
    assert (storage.IMAGE_DIR).is_dir()

    with closing(storage.get_connection()) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(clipboard_items)")}
    assert {"clipboard_items", "clip_vectors"} <= tables
    assert {"is_pinned", "pinned_at", "category"} <= columns


def test_legacy_db_is_migrated():
    """老库（没有 is_pinned/pinned_at/category 与向量表）应被自动补列。"""
    # 构造一个「只有最初七列」的老库
    storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if storage.DB_PATH.exists():
        storage.DB_PATH.unlink()
    with closing(sqlite3.connect(storage.DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE clipboard_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_type TEXT NOT NULL,
                text_content TEXT,
                image_path TEXT,
                thumbnail_path TEXT,
                content_hash TEXT NOT NULL UNIQUE,
                source_app TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO clipboard_items (content_type, content_hash, created_at)"
            " VALUES ('text', 'legacy-hash', '2026-01-01 00:00:00')"
        )
        conn.commit()

    storage.init_db()  # 触发迁移

    with closing(storage.get_connection()) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(clipboard_items)")}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = conn.execute(
            "SELECT is_pinned, category FROM clipboard_items WHERE content_hash = 'legacy-hash'"
        ).fetchone()
    assert {"is_pinned", "pinned_at", "category"} <= columns
    assert "clip_vectors" in tables
    # 老行补齐默认值：未置顶、无分类
    assert row[0] == 0 and row[1] is None


# ---------------------------------------------------------------------------
# 插入与去重
# ---------------------------------------------------------------------------


def test_insert_item_and_find_by_hash():
    item_id = storage.insert_item(
        "text", content_hash="hash-1", text_content="hello", source_app="Notepad"
    )
    assert isinstance(item_id, int)
    found = storage.find_by_hash("hash-1")
    assert found is not None
    assert found["text_content"] == "hello"
    assert storage.find_by_hash("missing") is None


def test_duplicate_hash_returns_none():
    """同哈希重复插入必须返回 None（不写第二行）。"""
    first = storage.insert_item("text", content_hash="dup", text_content="a")
    second = storage.insert_item("text", content_hash="dup", text_content="b")
    assert first is not None
    assert second is None
    with closing(storage.get_connection()) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM clipboard_items WHERE content_hash = 'dup'"
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def test_to_relative_inside_project():
    """数据目录内的路径应转成 posix 相对路径（以数据目录为基准）。"""
    path = storage.IMAGE_DIR / "abc.png"
    rel = storage.to_relative(path)
    assert rel == "images/abc.png"
    assert not Path(rel).is_absolute()


def test_to_relative_outside_project_falls_back():
    """项目外的路径退化为原始 posix 串（不抛异常）。"""
    outside = Path("C:/somewhere/else/x.png")
    rel = storage.to_relative(outside)
    assert isinstance(rel, str)


# ---------------------------------------------------------------------------
# 分类与向量
# ---------------------------------------------------------------------------


def test_set_category():
    item_id = storage.insert_item("text", content_hash="c1", text_content="t")
    storage.set_category(item_id, "代码")
    assert storage.find_by_hash("c1")["category"] == "代码"


def test_vector_roundtrip():
    item_id = storage.insert_item("text", content_hash="v1", text_content="t")
    vector = [0.1, 0.2, 0.3, -0.4]
    storage.upsert_vector(item_id, vector, "test-model")
    rows = storage.load_vectors()
    assert len(rows) == 1
    stored_id, stored_vector = rows[0]
    assert stored_id == item_id
    assert stored_vector == pytest.approx(vector, abs=1e-6)

    # 再写一次（幂等 upsert，不产生第二行）
    storage.upsert_vector(item_id, [1.0, 0.0, 0.0, 0.0], "test-model")
    assert len(storage.load_vectors()) == 1


def test_items_without_vector_only_returns_text():
    text_id = storage.insert_item("text", content_hash="t1", text_content="t")
    image_id = storage.insert_item(
        "image", content_hash="i1", image_path="a.png", thumbnail_path="thumb_a.png"
    )
    ids = [row["id"] for row in storage.items_without_vector()]
    assert text_id in ids
    assert image_id not in ids

    storage.upsert_vector(text_id, [1.0], "m")
    assert text_id not in [row["id"] for row in storage.items_without_vector()]


def test_stats_counts():
    storage.insert_item("text", content_hash="s1", text_content="t")
    storage.insert_item(
        "image", content_hash="s2", image_path="a.png", thumbnail_path="thumb_a.png"
    )
    stats = storage.stats()
    assert stats["total"] == 2
    assert stats["text_total"] == 1
    assert stats["pending_vectors"] == 1  # 唯一的文本还没有向量


# ---------------------------------------------------------------------------
# 列表 / 搜索 / 置顶 / 删除（GUI 直接依赖）
# ---------------------------------------------------------------------------


def test_list_items_order_pinned_first():
    """列表顺序：置顶最前，其余按创建时间倒序。"""
    old = storage.insert_item("text", content_hash="l1", text_content="旧")
    new = storage.insert_item("text", content_hash="l2", text_content="新")
    pinned = storage.insert_item("text", content_hash="l3", text_content="置顶的")
    storage.update_pin(pinned, True)

    ids = [row["id"] for row in storage.list_items()]
    assert ids[0] == pinned  # 置顶第一
    assert set(ids[1:]) == {old, new}  # 其余随后

    storage.update_pin(pinned, False)
    assert storage.get_item(pinned)["is_pinned"] == 0
    assert storage.get_item(pinned)["pinned_at"] is None


def test_list_items_keyword_search_and_escape():
    """关键词搜索：命中文本/来源；% 不会被当成通配符。"""
    storage.insert_item(
        "text", content_hash="k1", text_content="100% 折扣 促销", source_app="Chrome"
    )
    storage.insert_item(
        "text", content_hash="k2", text_content="普通会议纪要", source_app="Notepad"
    )

    assert len(storage.list_items(q="折扣")) == 1
    assert len(storage.list_items(q="Chrome")) == 1  # 来源应用也可搜
    assert len(storage.list_items(q="100%")) == 1  # 通配符已转义
    # 单独 % 只匹配字面含 % 的条目（第 1 条），不会退化成「匹配全部」
    assert len(storage.list_items(q="%")) == 1
    assert len(storage.list_items(q="_")) == 0  # _ 同理（没有条目含字面下划线）


def test_list_items_type_filter():
    storage.insert_item("text", content_hash="f1", text_content="t")
    storage.insert_item(
        "image", content_hash="f2", image_path="a.png", thumbnail_path="thumb_a.png"
    )
    assert len(storage.list_items(content_type="text")) == 1
    assert len(storage.list_items(content_type="image")) == 1
    assert storage.list_items(content_type="image")[0]["content_hash"] == "f2"


def test_get_item_and_delete_item():
    item_id = storage.insert_item(
        "image",
        content_hash="d1",
        image_path="images/x.png",
        thumbnail_path="images/thumb_x.png",
    )
    storage.upsert_vector(item_id, [1.0], "m")

    row = storage.get_item(item_id)
    assert row is not None and row["image_path"] == "images/x.png"
    assert storage.get_item(999999) is None

    paths = storage.delete_item(item_id)
    assert set(paths) == {"images/x.png", "images/thumb_x.png"}
    assert storage.get_item(item_id) is None
    assert storage.load_vectors() == []  # 向量同步清理
    assert storage.delete_item(item_id) == []  # 重复删除安全


def test_find_by_ids_preserves_lookup():
    a = storage.insert_item("text", content_hash="b1", text_content="a")
    b = storage.insert_item("text", content_hash="b2", text_content="b")
    row_map = storage.find_by_ids([b, a])
    assert set(row_map.keys()) == {a, b}
    assert row_map[a]["content_hash"] == "b1"
