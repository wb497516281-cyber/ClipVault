"""tests/test_rules.py — 规则引擎测试：API 密钥检测 -> 自动入 API 组。

AI 检测用 monkeypatch 替换，不发起真实请求。
"""

from __future__ import annotations

import pytest

import ai_client
import config
import rules
import storage


@pytest.fixture(autouse=True)
def clean_settings():
    """每个用例前后清空 GUI 设置，保证环境隔离。"""
    config.clear_settings()
    yield
    config.clear_settings()


def _item(hash_: str, text: str) -> int:
    return storage.insert_item("text", content_hash=hash_, text_content=text)


# ---------------------------------------------------------------------------
# detect_api_key 的是/否解析
# ---------------------------------------------------------------------------


def test_parse_yes_no_variants():
    assert ai_client._parse_yes_no("是") is True
    assert ai_client._parse_yes_no("是。") is True
    assert ai_client._parse_yes_no("YES") is True
    assert ai_client._parse_yes_no("是的，这是一个 API 密钥") is True
    assert ai_client._parse_yes_no("否") is False
    assert ai_client._parse_yes_no("不是") is False
    assert ai_client._parse_yes_no("No.") is False
    assert ai_client._parse_yes_no("无法确定") is None  # 拿不准
    assert ai_client._parse_yes_no("") is None


def test_detect_api_key_true(fake_key, monkeypatch):
    monkeypatch.setattr(
        ai_client, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "是"}}]}
    )
    assert ai_client.detect_api_key("sk-abc123") is True


def test_detect_api_key_false(fake_key, monkeypatch):
    monkeypatch.setattr(
        ai_client, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "否"}}]}
    )
    assert ai_client.detect_api_key("普通文本") is False


def test_detect_api_key_uncertain_is_false(fake_key, monkeypatch):
    """模型答非所问：安全侧返回 False（宁可漏检也不错分）。"""
    monkeypatch.setattr(
        ai_client, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "嗯……"}}]}
    )
    assert ai_client.detect_api_key("sk-abc123") is False


def test_detect_api_key_not_configured(monkeypatch):
    monkeypatch.delenv(ai_client.ENV_PREFIX + "API_KEY", raising=False)
    assert ai_client.detect_api_key("sk-abc123") is False


def test_detect_api_key_network_failure(fake_key, monkeypatch):
    monkeypatch.setattr(ai_client, "_post_json", lambda *a, **k: None)
    assert ai_client.detect_api_key("sk-abc123") is False


@pytest.fixture
def fake_key(monkeypatch):
    """模拟「系统环境已配置 API Key」场景。"""
    monkeypatch.setenv(ai_client.ENV_PREFIX + "API_KEY", "test-key-123")
    monkeypatch.setenv(ai_client.ENV_PREFIX + "ENABLED", "1")
    return monkeypatch


# ---------------------------------------------------------------------------
# apply_rule：命中入组 / 不动已有分组 / 失败安全侧
# ---------------------------------------------------------------------------


def test_apply_rule_hit_creates_api_group(monkeypatch):
    """命中：自动创建 API 组并入组。"""
    item_id = _item("r-1", "sk-abc123def456")
    monkeypatch.setattr(ai_client, "detect_api_key", lambda text: True)

    assert rules.apply_rule(item_id, "sk-abc123def456") is True

    group = next(g for g in storage.list_groups() if g["name"] == rules.API_RULE_GROUP)
    assert group["id"] in storage.item_group_ids(item_id)


def test_apply_rule_reuses_existing_api_group(monkeypatch):
    """API 组已存在：直接复用，不重复建组。"""
    _item("r-2a", "无关内容")
    existing = storage.create_group(rules.API_RULE_GROUP)
    item_id = _item("r-2b", "sk-xyz")
    monkeypatch.setattr(ai_client, "detect_api_key", lambda text: True)

    assert rules.apply_rule(item_id, "sk-xyz") is True

    assert len([g for g in storage.list_groups() if g["name"] == rules.API_RULE_GROUP]) == 1
    assert storage.item_group_ids(item_id) == [existing]


def test_apply_rule_skips_already_grouped(monkeypatch):
    """已有分组：尊重用户意图，规则不碰。"""
    item_id = _item("r-3", "sk-abc")
    other = storage.create_group("其他")
    storage.add_item_to_group(item_id, other)
    monkeypatch.setattr(ai_client, "detect_api_key", lambda text: True)

    assert rules.apply_rule(item_id, "sk-abc") is False
    assert storage.item_group_ids(item_id) == [other]  # 原样不动


def test_apply_rule_miss(monkeypatch):
    """未命中：不入组。"""
    item_id = _item("r-4", "普通文本")
    monkeypatch.setattr(ai_client, "detect_api_key", lambda text: False)
    assert rules.apply_rule(item_id, "普通文本") is False
    assert storage.item_group_ids(item_id) == []


def test_apply_rule_detection_exception_is_safe(monkeypatch):
    """检测抛异常：安全返回 False，不崩。"""

    def boom(_text):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(ai_client, "detect_api_key", boom)
    item_id = _item("r-5", "sk-abc")
    assert rules.apply_rule(item_id, "sk-abc") is False
    assert storage.item_group_ids(item_id) == []


def test_apply_rule_empty_text(monkeypatch):
    item_id = _item("r-6", "")
    monkeypatch.setattr(ai_client, "detect_api_key", lambda text: True)
    assert rules.apply_rule(item_id, "") is False


# ---------------------------------------------------------------------------
# run_rules：批量
# ---------------------------------------------------------------------------


def test_run_rules_counts_and_isolates_failures(monkeypatch):
    """批量跑：统计入组数；单条失败不影响其他条。"""
    a = _item("r-7a", "sk-aaa")
    b = _item("r-7b", "普通文本")
    c = _item("r-7c", "sk-ccc")
    seen: list[str] = []

    def fake_detect(text: str) -> bool:
        seen.append(text)
        if text == "sk-boom":  # 不存在，只是防御
            raise RuntimeError("x")
        return text.startswith("sk-")

    monkeypatch.setattr(ai_client, "detect_api_key", fake_detect)
    result = rules.run_rules([(a, "sk-aaa"), (b, "普通文本"), (c, "sk-ccc")])

    assert result == {"applied": 2, "total": 3}
    assert len(seen) == 3
    api_group = next(g for g in storage.list_groups() if g["name"] == rules.API_RULE_GROUP)
    assert api_group["id"] in storage.item_group_ids(a)
    assert api_group["id"] in storage.item_group_ids(c)
    assert storage.item_group_ids(b) == []


def test_run_rules_single_failure_does_not_stop_batch(monkeypatch):
    """某条检测抛异常：该条跳过，后续条照常处理。"""
    a = _item("r-8a", "sk-aaa")
    b = _item("r-8b", "sk-bbb")

    def fake_detect(text: str) -> bool:
        if text == "sk-aaa":
            raise RuntimeError("第一条炸了")
        return True

    monkeypatch.setattr(ai_client, "detect_api_key", fake_detect)
    result = rules.run_rules([(a, "sk-aaa"), (b, "sk-bbb")])

    assert result["applied"] == 1  # b 成功，a 跳过
    api_group = next(g for g in storage.list_groups() if g["name"] == rules.API_RULE_GROUP)
    assert api_group["id"] in storage.item_group_ids(b)


def test_rule_text_mentions_api_group():
    text = rules.rule_text()
    assert "API" in text and "未分组" in text
