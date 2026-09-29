"""tests/test_cleanup.py — 历史上限清理测试：未分组每周清、分组永久保留。

依赖：pytest；无需网络与剪贴板。图片操作用临时数据目录里的真实文件。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import cleanup
import storage


@pytest.fixture(autouse=True)
def fresh_cleanup_state(monkeypatch):
    """每个用例：清状态文件 + 重置进程内节流（否则 maybe_run 被小时级节流挡住）。"""
    cleanup._last_check_monotonic = None
    state = cleanup.state_path()
    state.unlink(missing_ok=True)
    yield
    state.unlink(missing_ok=True)
    cleanup._last_check_monotonic = None


def _write_state(days_ago: float) -> None:
    """把状态文件的 last_cleanup 写成 days_ago 天前。"""
    past = datetime.now() - timedelta(days=days_ago)
    cleanup.save_state(past.strftime("%Y-%m-%d %H:%M:%S"), 0)


def _seed_image() -> int:
    """插一条带真实图片文件的图片条目，返回 id。"""
    from PIL import Image

    storage.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (10, 10), (1, 2, 3))
    img.save(storage.IMAGE_DIR / "probe.png")
    thumb = Image.new("RGB", (10, 10), (1, 2, 3))
    thumb.save(storage.IMAGE_DIR / "thumb_probe.png")
    item_id = storage.insert_item(
        "image",
        content_hash="cleanup-img",
        image_path=storage.to_relative(storage.IMAGE_DIR / "probe.png"),
        thumbnail_path=storage.to_relative(storage.IMAGE_DIR / "thumb_probe.png"),
    )
    # 补一条语义向量，验证清理会连带删
    storage.upsert_vector(item_id, [1.0, 0.0], "test-model")
    return item_id


# ---------------------------------------------------------------------------
# 存储层：delete_ungrouped_items
# ---------------------------------------------------------------------------


def test_delete_ungrouped_items_keeps_grouped():
    """删未分组、留分组；置顶但未分组的一并删（置顶不是保留信号）。"""
    grouped = storage.insert_item("text", content_hash="c1", text_content="入组的")
    ungrouped = storage.insert_item("text", content_hash="c2", text_content="散养的")
    pinned = storage.insert_item("text", content_hash="c3", text_content="置顶没入组")
    storage.update_pin(pinned, True)
    group = storage.create_group("收藏")
    storage.add_item_to_group(grouped, group)

    count, paths = storage.delete_ungrouped_items()

    assert count == 2
    assert paths == []
    assert storage.get_item(grouped) is not None  # 分组的留下
    assert storage.get_item(ungrouped) is None
    assert storage.get_item(pinned) is None  # 置顶未分组也删


def test_delete_ungrouped_items_removes_vectors_and_reports_paths():
    """清理删向量，并把图片相对路径交回调用方删文件。"""
    _seed_image()
    assert len(storage.load_vectors()) == 1

    count, paths = storage.delete_ungrouped_items()

    assert count == 1
    assert set(paths) == {"images/probe.png", "images/thumb_probe.png"}
    assert storage.load_vectors() == []  # 向量连带删掉


def test_delete_ungrouped_items_empty_db():
    """空库/全部分组时删除 0 条，不抛异常。"""
    grouped = storage.insert_item("text", content_hash="c4", text_content="x")
    group = storage.create_group("g")
    storage.add_item_to_group(grouped, group)
    assert storage.delete_ungrouped_items() == (0, [])


# ---------------------------------------------------------------------------
# 清理模块：到期判断 / 状态文件 / 执行
# ---------------------------------------------------------------------------


def test_ensure_state_registers_clock_without_deleting():
    """升级首启：只登记时间，不删任何数据（7 天后才第一次真扫）。"""
    ungrouped = storage.insert_item("text", content_hash="c5", text_content="老历史")

    cleanup.ensure_state()

    assert cleanup.state_path().is_file()
    assert cleanup.is_due() is False  # 刚登记，未到期
    assert storage.get_item(ungrouped) is not None  # 一条没动


def test_is_due_after_seven_days():
    """状态是 7 天前 → 到期；3 天前 → 没到期。"""
    _write_state(7.1)
    assert cleanup.is_due() is True
    _write_state(3)
    assert cleanup.is_due() is False


def test_get_cleanup_days_env_override(monkeypatch):
    """CLIPVAULT_CLEANUP_DAYS 可覆盖间隔；非法值回落默认。"""
    assert cleanup.get_cleanup_days() == 7
    monkeypatch.setenv("CLIPVAULT_CLEANUP_DAYS", "3")
    assert cleanup.get_cleanup_days() == 3
    monkeypatch.setenv("CLIPVAULT_CLEANUP_DAYS", "abc")
    assert cleanup.get_cleanup_days() == 7  # 非法回落
    monkeypatch.setenv("CLIPVAULT_CLEANUP_DAYS", "0")
    assert cleanup.get_cleanup_days() == 7  # 非正数回落


def test_run_cleanup_skipped_when_not_due():
    """没到期就跳过：一条不删，状态不变。"""
    _write_state(1)
    ungrouped = storage.insert_item("text", content_hash="c6", text_content="还活着")

    result = cleanup.run_cleanup()

    assert result == {"deleted": 0, "ran": False, "reason": "未到清理周期"}
    assert storage.get_item(ungrouped) is not None


def test_run_cleanup_deletes_and_updates_state():
    """到期清理：删未分组（含图片文件），状态文件更新。"""
    _write_state(8)
    ungrouped = storage.insert_item("text", content_hash="c7", text_content="该走了")
    grouped = storage.insert_item("text", content_hash="c8", text_content="留下了")
    group = storage.create_group("收藏")
    storage.add_item_to_group(grouped, group)
    image_id = _seed_image()

    result = cleanup.run_cleanup()

    assert result["ran"] is True and result["deleted"] == 2
    assert storage.get_item(ungrouped) is None
    assert storage.get_item(grouped) is not None
    # 图片文件也被删掉（IMAGE_DIR 内）
    assert not (storage.IMAGE_DIR / "probe.png").exists()
    assert not (storage.IMAGE_DIR / "thumb_probe.png").exists()
    # 状态：last_cleanup 刷新为现在，last_deleted 记录条数
    state = cleanup.load_state()
    assert state["last_deleted"] == 2
    assert datetime.strptime(state["last_cleanup"], "%Y-%m-%d %H:%M:%S") > datetime.now() - timedelta(minutes=1)
    # 清理后又没到期了
    assert cleanup.is_due() is False
    assert image_id is not None


def test_run_cleanup_force_ignores_due():
    """手动清理（force=True）：没到期也立即执行。"""
    _write_state(1)
    ungrouped = storage.insert_item("text", content_hash="c9", text_content="手动再见")

    result = cleanup.run_cleanup(force=True)

    assert result["ran"] is True and result["deleted"] == 1
    assert storage.get_item(ungrouped) is None


def test_maybe_run_throttles_and_runs_when_due(monkeypatch):
    """maybe_run：进程内节流；到期时执行并返回结果，没到期返回 None。"""
    _write_state(8)  # 已到期
    ungrouped = storage.insert_item("text", content_hash="c10", text_content="到期清理")

    first = cleanup.maybe_run()
    assert first is not None and first["deleted"] == 1
    assert storage.get_item(ungrouped) is None

    # 紧接着再调：进程内节流挡住（一小时才查一次状态文件）
    assert cleanup.maybe_run() is None


def test_maybe_run_returns_none_when_not_due():
    """没到期：maybe_run 直接返回 None，不碰数据。"""
    cleanup.ensure_state()
    ungrouped = storage.insert_item("text", content_hash="c11", text_content="不动我")
    assert cleanup.maybe_run() is None
    assert storage.get_item(ungrouped) is not None


def test_next_cleanup_text():
    """下次清理时间文案：没登记过给升级提示，登记过给具体日期。"""
    assert "7 天" in cleanup.next_cleanup_text()
    _write_state(2)
    text = cleanup.next_cleanup_text()
    assert "下次自动清理" in text
    # 2 天前 + 7 天周期 = 5 天后的日期
    assert (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d") in text


def test_state_survives_corrupt_file():
    """状态文件损坏：视为从未清理（容错），不崩。"""
    cleanup.state_path().parent.mkdir(parents=True, exist_ok=True)
    cleanup.state_path().write_text("{ 坏掉的 json", encoding="utf-8")
    assert cleanup.load_state() == {}
    assert cleanup.last_cleanup_time() is None
