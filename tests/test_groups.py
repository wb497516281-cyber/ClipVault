"""tests/test_groups.py — 分组功能测试：存储层分组关系 + AI 分组解析/分配。

依赖：pytest；无需剪贴板与网络（AI 调用全部 monkeypatch 替换）。
"""

from __future__ import annotations

from contextlib import closing

import pytest

import ai_client
import config
import storage


@pytest.fixture(autouse=True)
def clean_settings():
    """每个用例前后清空 GUI 设置，保证环境隔离。"""
    config.clear_settings()
    yield
    config.clear_settings()


def _text(hash_: str, content: str = "一些文本内容") -> int:
    """插一条文本条目，返回 id。"""
    return storage.insert_item("text", content_hash=hash_, text_content=content)


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------


def test_init_db_creates_group_tables():
    """init_db 应创建分组相关的两张表。"""
    storage.init_db()
    with closing(storage.get_connection()) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"clip_groups", "clip_group_members"} <= tables


# ---------------------------------------------------------------------------
# 分组 CRUD
# ---------------------------------------------------------------------------


def test_create_and_list_groups():
    """新建分组：名称/计数正确，新分组排在前。"""
    g1 = storage.create_group("工作")
    g2 = storage.create_group("代码")
    groups = {g["id"]: g for g in storage.list_groups()}
    assert groups[g1]["name"] == "工作" and groups[g1]["count"] == 0
    assert groups[g2]["name"] == "代码"
    assert [g["id"] for g in storage.list_groups()] == [g2, g1]  # position 倒序
    assert storage.get_group(g1)["name"] == "工作"
    assert storage.get_group(99999) is None


def test_create_group_rejects_empty_and_duplicate():
    """空名与重名都应被拒绝（抛 ValueError）。"""
    storage.create_group("工作")
    with pytest.raises(ValueError, match="已存在"):
        storage.create_group("工作")
    with pytest.raises(ValueError, match="不能为空"):
        storage.create_group("   ")


def test_rename_group():
    """重命名：生效；空名拒绝。"""
    group = storage.create_group("旧名")
    storage.rename_group(group, "新名")
    assert storage.get_group(group)["name"] == "新名"
    with pytest.raises(ValueError):
        storage.rename_group(group, "")


def test_delete_group_keeps_items():
    """删分组只删分组行与成员关系，条目本身保留。"""
    text_id = _text("g-1")
    group = storage.create_group("临时")
    storage.add_item_to_group(text_id, group)
    storage.delete_group(group)
    assert storage.get_group(group) is None
    assert storage.get_item(text_id) is not None
    assert storage.item_group_ids(text_id) == []


# ---------------------------------------------------------------------------
# 成员关系
# ---------------------------------------------------------------------------


def test_membership_add_remove_return_values():
    """加入/移出的返回值语义：True=状态真的变了，False=本就如此。"""
    text_id = _text("g-2")
    group = storage.create_group("工作")
    assert storage.add_item_to_group(text_id, group) is True
    assert storage.add_item_to_group(text_id, group) is False  # 重复加入
    assert storage.item_group_ids(text_id) == [group]
    assert storage.remove_item_from_group(text_id, group) is True
    assert storage.remove_item_from_group(text_id, group) is False  # 已不在组里


def test_item_can_belong_to_multiple_groups():
    """一条记录可同时属于多个分组；set_item_groups 是替换语义。"""
    text_id = _text("g-3")
    g1 = storage.create_group("工作")
    g2 = storage.create_group("代码")
    storage.set_item_groups(text_id, [g1, g2])
    assert set(storage.item_group_ids(text_id)) == {g1, g2}
    storage.set_item_groups(text_id, [g2])  # 只保留 g2
    assert storage.item_group_ids(text_id) == [g2]
    storage.set_item_groups(text_id, [])  # 清空
    assert storage.item_group_ids(text_id) == []


def test_set_item_groups_filters_unknown_ids():
    """不存在的分组 id 被静默过滤，不会写进成员表。"""
    text_id = _text("g-4")
    group = storage.create_group("工作")
    storage.set_item_groups(text_id, [group, 99999])
    assert storage.item_group_ids(text_id) == [group]


def test_group_names_by_ids():
    """批量取分组名（卡片徽章用）；空输入返回空字典。"""
    t1, t2 = _text("g-5"), _text("g-6")
    g1 = storage.create_group("工作")
    g2 = storage.create_group("代码")
    storage.add_item_to_group(t1, g1)
    storage.add_item_to_group(t2, g1)
    storage.add_item_to_group(t2, g2)
    names = storage.group_names_by_ids([t1, t2])
    assert names[t1] == ["工作"]
    assert set(names[t2]) == {"工作", "代码"}
    assert storage.group_names_by_ids([]) == {}


# ---------------------------------------------------------------------------
# 列表过滤与统计
# ---------------------------------------------------------------------------


def test_list_items_filter_by_group_and_ungrouped():
    """分组过滤：单组 / 未分组 / 已分组三种口径。"""
    t1, t2 = _text("g-7"), _text("g-8")
    group = storage.create_group("工作")
    storage.add_item_to_group(t1, group)
    assert [r["id"] for r in storage.list_items(group_id=group)] == [t1]
    assert [r["id"] for r in storage.list_items(grouped="ungrouped")] == [t2]
    assert [r["id"] for r in storage.list_items(grouped="grouped")] == [t1]
    assert len(storage.list_items()) == 2  # 不传过滤参数时一切照旧


def test_list_items_group_filter_with_search():
    """分组过滤与关键词搜索可叠加。"""
    _text("g-9", "hello world")
    t2 = _text("g-10", "hello python")
    group = storage.create_group("代码")
    storage.add_item_to_group(t2, group)
    rows = storage.list_items(q="hello", group_id=group)
    assert [r["id"] for r in rows] == [t2]


def test_group_overview_counts():
    """总览：总条数 / 未分组数 / 每组计数。"""
    t1, t2, t3 = _text("g-11"), _text("g-12"), _text("g-13")
    g1 = storage.create_group("工作")
    g2 = storage.create_group("代码")
    storage.add_item_to_group(t1, g1)
    storage.add_item_to_group(t2, g1)
    storage.add_item_to_group(t3, g2)
    overview = storage.group_overview()
    assert overview["total"] == 3
    assert overview["ungrouped"] == 0
    counts = {g["id"]: g["count"] for g in overview["groups"]}
    assert counts[g1] == 2 and counts[g2] == 1


def test_ungrouped_text_items_excludes_grouped_and_images():
    """AI 分组池：只要「未分组 + 文本」条目。"""
    t1 = _text("g-14")
    t2 = _text("g-15")
    storage.insert_item("image", content_hash="g-16", source_app="Explorer")  # 图片不进池
    group = storage.create_group("工作")
    storage.add_item_to_group(t1, group)
    assert [r["id"] for r in storage.ungrouped_text_items()] == [t2]


# ---------------------------------------------------------------------------
# AI 分组：解析容错与批量分配（网络全部 monkeypatch）
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_key(monkeypatch):
    """模拟「系统环境已配置 API Key」场景。"""
    monkeypatch.setenv(ai_client.ENV_PREFIX + "API_KEY", "test-key-123")
    monkeypatch.setenv(ai_client.ENV_PREFIX + "ENABLED", "1")
    return monkeypatch


def test_parse_group_assignment_basic():
    """标准 JSON 数组直接解析。"""
    content = '[{"id": 1, "group": "工作"}, {"id": 2, "group": "代码"}]'
    assert ai_client.parse_group_assignment(content, {1, 2}) == {1: "工作", 2: "代码"}


def test_parse_group_assignment_fenced_and_noise(fake_key):
    """带 ```json 围栏和前后废话也能解析。"""
    content = (
        "好的，分组结果如下：\n"
        "```json\n"
        '[{"id": 1, "group": "工作"}]\n'
        "```\n以上就是结果。"
    )
    assert ai_client.parse_group_assignment(content, {1}) == {1: "工作"}


def test_parse_group_assignment_dict_form():
    """"{id: group}" 字典形态也支持。"""
    content = '{"1": "工作", "2": "代码"}'
    assert ai_client.parse_group_assignment(content, {1, 2}) == {1: "工作", 2: "代码"}


def test_parse_group_assignment_filters_invalid_entries():
    """非法条目一律过滤：幽灵 id / 缺 id / 空名 / 超长名。"""
    content = (
        '[{"id": 1, "group": "工作"},'
        ' {"id": 999, "group": "幽灵"},'  # 不在本批 id 里
        ' {"group": "无编号"},'  # 缺 id
        ' {"id": 2, "group": ""},'  # 空分组名
        ' {"id": 3, "group": "'
        + "x" * 31  # 超长名（31 字符，超过 30 上限）
        + '"}]'
    )
    assert ai_client.parse_group_assignment(content, {1, 2, 3}) == {1: "工作"}


def test_parse_group_assignment_invalid_json_returns_empty():
    """整段不是合法 JSON（如引号未闭合/乘法漏出字符串）时整体跳过，不抛异常。"""
    content = '[{"id": 1, "group": "x" * 31}]'  # 模拟模型输出的坏 JSON
    assert ai_client.parse_group_assignment(content, {1}) == {}


def test_parse_group_assignment_garbage_returns_empty():
    """完全不是 JSON / 空内容：返回空字典，不抛异常。"""
    assert ai_client.parse_group_assignment("完全不是 JSON", {1}) == {}
    assert ai_client.parse_group_assignment("", {1}) == {}


def test_parse_group_assignment_limits_new_groups():
    """新分组名数量超限时，多出的分配被丢弃（宁可少分也不错分）。"""
    content = (
        '[{"id": 1, "group": "A"}, {"id": 2, "group": "B"},'
        ' {"id": 3, "group": "C"}, {"id": 4, "group": "D"}]'
    )
    result = ai_client.parse_group_assignment(content, {1, 2, 3, 4}, max_new_groups=2)
    assert result == {1: "A", 2: "B"}


def test_assign_groups_batches_and_merges(fake_key, monkeypatch):
    """50 条 -> 2 批（40 + 10）；后一批沿用前一批新建的分组名。"""
    calls = []

    def fake_post(path, payload):
        calls.append(len(payload["messages"][1]["content"].splitlines()))
        idx = 1 if len(calls) == 1 else 41
        content = '[{"id": ' + str(idx) + ', "group": "工作"}]'
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(ai_client, "_post_json", fake_post)
    rows = [(i, f"内容{i}") for i in range(1, 51)]
    result = ai_client.assign_groups(rows, [])
    assert result == {1: "工作", 41: "工作"}
    assert calls == [40, 10]


def test_assign_groups_skips_failed_batch(fake_key, monkeypatch):
    """某批网络失败只丢该批，其余批次结果照常生效。"""
    calls = []

    def fake_post(path, payload):
        calls.append(1)
        if len(calls) == 1:
            return None  # 第一批失败
        return {"choices": [{"message": {"content": '[{"id": 41, "group": "代码"}]'}}]}

    monkeypatch.setattr(ai_client, "_post_json", fake_post)
    rows = [(i, f"内容{i}") for i in range(1, 51)]
    assert ai_client.assign_groups(rows, []) == {41: "代码"}


def test_assign_groups_empty_input_short_circuits(fake_key, monkeypatch):
    """空输入不发请求。"""
    monkeypatch.setattr(ai_client, "_post_json", lambda *a, **k: pytest.fail("不应发起请求"))
    assert ai_client.assign_groups([], []) == {}


def test_assign_groups_not_configured_returns_empty(monkeypatch):
    """未配置 AI：整体安全降级，不起任何请求。"""
    monkeypatch.delenv(ai_client.ENV_PREFIX + "API_KEY", raising=False)
    rows = [(1, "内容")]
    assert ai_client.assign_groups(rows, ["工作"]) == {}
    assert ai_client.suggest_group("内容") is None


def test_suggest_group_returns_name(fake_key, monkeypatch):
    """单条建议：直接拿分组名。"""
    monkeypatch.setattr(
        ai_client,
        "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": '[{"id": 0, "group": "工作"}]'}}]},
    )
    assert ai_client.suggest_group("https://example.com", ["工作"]) == "工作"


def test_preview_for_grouping_flattens_and_truncates():
    """摘要：换行压平 + 截断。"""
    assert ai_client._preview_for_grouping("a\n\nb\tc") == "a b c"
    assert len(ai_client._preview_for_grouping("x" * 500, max_chars=10)) == 10
