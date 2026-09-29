"""tests/test_storage.py — 存储层测试：建表/迁移/插入/去重/路径/向量。

依赖：pytest；无需剪贴板与网络。
"""

from __future__ import annotations

import hashlib
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


def test_list_items_search_matches_custom_title():
    """关键词搜索必须命中自定义命名（title）：内容没关键词但名字有，也要搜得到。"""
    named = storage.insert_item("text", content_hash="k3", text_content="sk-abc123 密钥")
    storage.update_item_title(named, "DeepSeek 账号")
    other = storage.insert_item("text", content_hash="k4", text_content="会议纪要")

    rows = storage.list_items(q="DeepSeek")
    assert [row["id"] for row in rows] == [named]  # 名字命中即返回

    # 命名 + 内容同时命中同一个词时只算一条（DISTINCT 兜底）
    storage.insert_item("text", content_hash="k5", text_content="DeepSeek 相关的笔记")
    assert len(storage.list_items(q="DeepSeek")) == 2
    assert other not in [row["id"] for row in storage.list_items(q="DeepSeek")]


def test_list_items_search_title_for_image_items():
    """图片条目没有文本内容，但命名同样可搜。"""
    image_id = storage.insert_item(
        "image", content_hash="k6", image_path="a.png", thumbnail_path="thumb_a.png"
    )
    storage.update_item_title(image_id, "设计稿-终版")
    assert [row["id"] for row in storage.list_items(q="终版")] == [image_id]


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


# ---------------------------------------------------------------------------
# 编辑：命名 / 改内容（GUI 编辑窗依赖）
# ---------------------------------------------------------------------------


def test_update_item_title_roundtrip():
    item_id = storage.insert_item("text", content_hash="t1", text_content="内容")
    storage.update_item_title(item_id, "  重要片段  ")
    assert storage.get_item(item_id)["title"] == "重要片段"
    # 空串 = 清除名称
    storage.update_item_title(item_id, "")
    assert storage.get_item(item_id)["title"] is None


def test_update_item_content_recomputes_hash():
    item_id = storage.insert_item("text", content_hash="c1", text_content="旧内容")
    storage.update_item_content(item_id, "新内容")
    row = storage.get_item(item_id)
    assert row["text_content"] == "新内容"
    assert row["content_hash"] == hashlib.sha256("新内容".encode()).hexdigest()
    # 按新内容能搜到
    assert len(storage.list_items(q="新内容")) == 1


def test_update_item_content_conflict():
    """改成的内容与另一条重复时拒绝修改。"""
    a = storage.insert_item(
        "text",
        content_hash=hashlib.sha256("甲".encode()).hexdigest(),
        text_content="甲",
    )
    b = storage.insert_item(
        "text",
        content_hash=hashlib.sha256("乙".encode()).hexdigest(),
        text_content="乙",
    )
    with pytest.raises(storage.ContentConflictError):
        storage.update_item_content(b, "甲")
    # 原内容保持不变
    assert storage.get_item(b)["text_content"] == "乙"
    assert storage.find_by_hash(hashlib.sha256("甲".encode()).hexdigest())["id"] == a


def test_update_item_content_rejects_empty():
    item_id = storage.insert_item("text", content_hash="e1", text_content="内容")
    with pytest.raises(ValueError):
        storage.update_item_content(item_id, "   ")


def test_update_item_content_rejects_image_item():
    """图片条目不允许改内容（防止 content_type 与 text_content 错乱）。"""
    image_id = storage.insert_item(
        "image", content_hash="e2", image_path="a.png", thumbnail_path="thumb_a.png"
    )
    with pytest.raises(ValueError):
        storage.update_item_content(image_id, "新内容")


def test_update_item_content_rejects_missing_item():
    """条目不存在时抛错（而不是静默 no-op 让 GUI 误报已保存）。"""
    with pytest.raises(ValueError):
        storage.update_item_content(999999, "新内容")


def test_update_item_content_clears_category():
    """改内容后旧分类失效：category 被清空（等 AI 重新分类）。"""
    item_id = storage.insert_item("text", content_hash="g1", text_content="旧")
    storage.set_category(item_id, "代码")
    storage.update_item_content(item_id, "新")
    assert storage.get_item(item_id)["category"] is None


def test_update_item_content_clears_vector():
    """内容变了旧向量失效：应被清掉（等 AI 队列重建）。"""
    item_id = storage.insert_item("text", content_hash="f1", text_content="旧")
    storage.upsert_vector(item_id, [1.0], "m")
    storage.update_item_content(item_id, "新")
    assert storage.load_vectors() == []


def test_legacy_db_gains_title_column():
    """老库（连 title 都没有）升级后应补齐 title 列。"""
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
        conn.commit()
    storage.init_db()
    with closing(storage.get_connection()) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(clipboard_items)")}
    assert "title" in columns


def test_delete_items_batch():
    """批量删除：行 + 向量 + 成员关系都清；返回图片路径；空列表安全。"""
    from PIL import Image

    storage.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (10, 10), (1, 2, 3))
    img.save(storage.IMAGE_DIR / "batch.png")
    image_id = storage.insert_item(
        "image",
        content_hash="batch-img",
        image_path="images/batch.png",
        thumbnail_path="images/thumb_batch.png",
    )
    text_id = storage.insert_item("text", content_hash="batch-text", text_content="会被删")
    keep_id = storage.insert_item("text", content_hash="batch-keep", text_content="保留")
    group = storage.create_group("g")
    storage.add_item_to_group(text_id, group)
    storage.add_item_to_group(keep_id, group)
    storage.upsert_vector(text_id, [1.0], "m")

    paths = storage.delete_items([image_id, text_id, 99999])  # 含不存在的 id

    assert set(paths) == {"images/batch.png", "images/thumb_batch.png"}
    assert storage.get_item(image_id) is None
    assert storage.get_item(text_id) is None
    assert storage.get_item(99999) is None
    assert storage.get_item(keep_id) is not None  # 没选中的保留
    assert storage.item_group_ids(keep_id) == [group]  # 保留条目的分组不动
    assert storage.load_vectors() == []  # 向量连带删
    assert storage.delete_items([]) == []  # 空列表安全
