"""tests/test_cleanup.py — 历史上限清理测试：未分组按每条保存年龄删、分组永久保留。

依赖：pytest；无需网络与剪贴板。年龄通过直接改 created_at 模拟。
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta

import pytest

import cleanup
import storage


@pytest.fixture(autouse=True)
def fresh_cleanup_state(monkeypatch):
    """每个用例：清状态文件 + 重置进程内节流（否则 maybe_run 被小时级节流挡住）。"""
    cleanup._last_run_monotonic = None
    state = cleanup.state_path()
    state.unlink(missing_ok=True)
    yield
    state.unlink(missing_ok=True)
    cleanup._last_run_monotonic = None


def _aged_item(hash_: str, days_old: float, content: str | None = None) -> int:
    """插一条文本条目并把 created_at 改成 days_old 天前（模拟年龄）。"""
    item_id = storage.insert_item(
        "text", content_hash=hash_, text_content=content or hash_
    )
    old = (datetime.now() - timedelta(days=days_old)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(storage.get_connection()) as conn:
        conn.execute(
            "UPDATE clipboard_items SET created_at = ? WHERE id = ?", (old, item_id)
        )
        conn.commit()
    return item_id


def _seed_image(days_old: float) -> int:
    """插一条带真实图片文件的图片条目（aged），返回 id。"""
    from PIL import Image

    storage.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (10, 10), (1, 2, 3))
    img.save(storage.IMAGE_DIR / "probe.png")
    img.save(storage.IMAGE_DIR / "thumb_probe.png")
    item_id = storage.insert_item(
        "image",
        content_hash="cleanup-img",
        image_path=storage.to_relative(storage.IMAGE_DIR / "probe.png"),
        thumbnail_path=storage.to_relative(storage.IMAGE_DIR / "thumb_probe.png"),
    )
    storage.upsert_vector(item_id, [1.0, 0.0], "test-model")
    old = (datetime.now() - timedelta(days=days_old)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(storage.get_connection()) as conn:
        conn.execute(
            "UPDATE clipboard_items SET created_at = ? WHERE id = ?", (old, item_id)
        )
        conn.commit()
    return item_id


# ---------------------------------------------------------------------------
# 存储层：按年龄删未分组
# ---------------------------------------------------------------------------


def test_delete_ungrouped_only_older_than_days():
    """按年龄删：8 天前的未分组删，3 天前的未分组留。"""
    old = _aged_item("c1", 8)
    young = _aged_item("c2", 3)

    count, _ = storage.delete_ungrouped_items(older_than_days=7)

    assert count == 1
    assert storage.get_item(old) is None
    assert storage.get_item(young) is not None


def test_delete_ungrouped_keeps_grouped_regardless_of_age():
    """入了组的永不删，哪怕比保留天数老得多。"""
    grouped = _aged_item("c3", 100)  # 100 天前，但入组了
    ungrouped_old = _aged_item("c4", 30)
    group = storage.create_group("收藏")
    storage.add_item_to_group(grouped, group)

    count, _ = storage.delete_ungrouped_items(older_than_days=7)

    assert count == 1
    assert storage.get_item(grouped) is not None
    assert storage.get_item(ungrouped_old) is None


def test_delete_ungrouped_without_age_deletes_all():
    """older_than_days=None（手动清理）：全部未分组都删，不看作没看过天数。"""
    fresh = storage.insert_item("text", content_hash="c5", text_content="刚复制的")
    old = _aged_item("c6", 30)

    count, _ = storage.delete_ungrouped_items(older_than_days=None)

    assert count == 2
    assert storage.get_item(fresh) is None
    assert storage.get_item(old) is None


def test_delete_ungrouped_pinned_also_deleted():
    """置顶但未分组的一并删（置顶是展示优先级，不是保留信号）。"""
    pinned = _aged_item("c7", 30)
    storage.update_pin(pinned, True)
    count, _ = storage.delete_ungrouped_items(older_than_days=7)
    assert count == 1 and storage.get_item(pinned) is None


def test_delete_ungrouped_removes_vectors_and_reports_paths():
    """清理删向量，并把图片相对路径交回调用方删文件。"""
    _seed_image(days_old=10)
    assert len(storage.load_vectors()) == 1

    count, paths = storage.delete_ungrouped_items(older_than_days=7)

    assert count == 1
    assert set(paths) == {"images/probe.png", "images/thumb_probe.png"}
    assert storage.load_vectors() == []


def test_delete_ungrouped_none_qualified():
    """没有够龄的未分组条目：删 0 条，不抛异常。"""
    _aged_item("c8", 1)  # 才 1 天
    grouped = _aged_item("c9", 30)
    group = storage.create_group("g")
    storage.add_item_to_group(grouped, group)
    assert storage.delete_ungrouped_items(older_than_days=7) == (0, [])


# ---------------------------------------------------------------------------
# 清理模块：自动（按年龄）/ 手动（全部）
# ---------------------------------------------------------------------------


def test_get_cleanup_days_default_and_env(monkeypatch):
    """保留天数：默认 7；CLIPVAULT_CLEANUP_DAYS 可调；非法值回落。"""
    assert cleanup.get_cleanup_days() == 7
    monkeypatch.setenv("CLIPVAULT_CLEANUP_DAYS", "3")
    assert cleanup.get_cleanup_days() == 3
    monkeypatch.setenv("CLIPVAULT_CLEANUP_DAYS", "abc")
    assert cleanup.get_cleanup_days() == 7
    monkeypatch.setenv("CLIPVAULT_CLEANUP_DAYS", "-1")
    assert cleanup.get_cleanup_days() == 7


def test_rule_text():
    """规则文案包含天数与『分组永久保留』。"""
    text = cleanup.rule_text()
    assert "7" in text and "分组" in text and "永久保留" in text


def test_run_auto_deletes_only_expired():
    """自动清理：只删未分组且满 N 天的；没够龄的未分组留着。"""
    expired = _aged_item("a1", 8)
    young = _aged_item("a2", 6)  # 差一天，留
    grouped = _aged_item("a3", 30)
    group = storage.create_group("收藏")
    storage.add_item_to_group(grouped, group)

    result = cleanup.run_auto()

    assert result["ran"] is True and result["deleted"] == 1
    assert storage.get_item(expired) is None
    assert storage.get_item(young) is not None  # 没满 7 天不动
    assert storage.get_item(grouped) is not None


def test_run_auto_respects_custom_days(monkeypatch):
    """保留天数改成 3：4 天前的未分组就该删了。"""
    monkeypatch.setenv("CLIPVAULT_CLEANUP_DAYS", "3")
    item = _aged_item("a4", 4)
    young = _aged_item("a5", 2)

    result = cleanup.run_auto()

    assert result["deleted"] == 1
    assert storage.get_item(item) is None
    assert storage.get_item(young) is not None


def test_run_auto_boundary_exactly_n_days():
    """边界：刚好 N 天前的条目会被删（created_at <= now - N 天）。"""
    boundary = _aged_item("a6", 7)  # 刚好 7 天（略早于此刻）
    result = cleanup.run_auto()
    assert result["deleted"] == 1
    assert storage.get_item(boundary) is None


def test_run_auto_deletes_image_files_and_writes_state():
    """自动清理删图片文件 + 状态文件记录。"""
    _seed_image(days_old=10)
    result = cleanup.run_auto()
    assert result["deleted"] == 1
    assert not (storage.IMAGE_DIR / "probe.png").exists()
    assert not (storage.IMAGE_DIR / "thumb_probe.png").exists()
    state = cleanup.load_state()
    assert state["last_deleted"] == 1
    assert datetime.strptime(state["last_run"], "%Y-%m-%d %H:%M:%S") > datetime.now() - timedelta(
        minutes=1
    )


def test_run_auto_nothing_to_delete_still_records():
    """没东西可删也登记一次运行（诊断用），deleted=0。"""
    _aged_item("a7", 1)
    result = cleanup.run_auto()
    assert result["ran"] is True and result["deleted"] == 0
    assert cleanup.load_state()["last_deleted"] == 0


def test_run_manual_deletes_all_ungrouped():
    """手动清理：全部未分组立即删，包括刚复制的。"""
    fresh = storage.insert_item("text", content_hash="a8", text_content="刚复制")
    old = _aged_item("a9", 30)
    grouped = _aged_item("a10", 30)
    group = storage.create_group("收藏")
    storage.add_item_to_group(grouped, group)

    result = cleanup.run_manual()

    assert result["deleted"] == 2
    assert storage.get_item(fresh) is None
    assert storage.get_item(old) is None
    assert storage.get_item(grouped) is not None


def test_maybe_run_throttles_then_deletes(monkeypatch):
    """maybe_run：进程内每小时最多真跑一次；跑了且删了才返回非 None。"""
    expired = _aged_item("m1", 8)

    first = cleanup.maybe_run()
    assert first == {"deleted": 1}
    assert storage.get_item(expired) is None

    # 紧接着再调：节流挡住（一小时才跑一次）
    assert cleanup.maybe_run() is None


def test_maybe_run_returns_none_when_nothing_deleted():
    """有未分组但都没够龄：跑了但删了 0 条 → 返回 None（不打扰用户）。"""
    _aged_item("m2", 2)
    assert cleanup.maybe_run() is None


def test_state_survives_corrupt_file():
    """状态文件损坏：返回空字典，不崩。"""
    cleanup.state_path().parent.mkdir(parents=True, exist_ok=True)
    cleanup.state_path().write_text("{ 坏掉的 json", encoding="utf-8")
    assert cleanup.load_state() == {}
