"""tests/test_ai_client.py — AI 客户端测试：降级、解析、相似度、后台队列。

所有网络调用都用 monkeypatch 替换，不会发起真实请求。
"""

from __future__ import annotations

import queue

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


@pytest.fixture
def fake_key(monkeypatch):
    """模拟「系统环境已配置 API Key」场景。"""
    monkeypatch.setenv(ai_client.ENV_PREFIX + "API_KEY", "test-key-123")
    monkeypatch.setenv(ai_client.ENV_PREFIX + "ENABLED", "1")
    return monkeypatch


# ---------------------------------------------------------------------------
# 配置探测与降级
# ---------------------------------------------------------------------------


def test_not_configured_without_key(monkeypatch):
    monkeypatch.delenv(ai_client.ENV_PREFIX + "API_KEY", raising=False)
    assert ai_client.is_configured() is False


def test_gui_settings_provide_key(fake_key):
    """界面设置（settings.json）里的 Key 应被识别为已配置。"""
    config.save_settings({ai_client.ENV_PREFIX + "API_KEY": "gui-key-123"})
    assert ai_client.get_api_key() == "gui-key-123"
    assert ai_client.is_configured() is True


def test_gui_settings_override_env(fake_key):
    """同名配置：界面设置优先于环境变量。"""
    assert ai_client.get_api_key() == "test-key-123"
    config.save_settings({ai_client.ENV_PREFIX + "API_KEY": "gui-key"})
    assert ai_client.get_api_key() == "gui-key"


def test_disabled_flag_wins(monkeypatch):
    monkeypatch.setenv(ai_client.ENV_PREFIX + "API_KEY", "k")
    monkeypatch.setenv(ai_client.ENV_PREFIX + "ENABLED", "0")
    assert ai_client.is_configured() is False


def test_disabled_via_gui_settings(fake_key):
    """界面里关掉「启用 AI」= ENABLED=0 存入 settings.json。"""
    config.save_settings(
        {
            ai_client.ENV_PREFIX + "API_KEY": "gui-key",
            ai_client.ENV_PREFIX + "ENABLED": "0",
        }
    )
    assert ai_client.is_configured() is False


def test_degrade_when_not_configured(monkeypatch):
    """未配置时所有能力安全返回 None，不抛异常。"""
    monkeypatch.delenv(ai_client.ENV_PREFIX + "API_KEY", raising=False)
    assert ai_client.classify_text("任意文本") is None
    assert ai_client.embed_text("任意文本") is None
    assert ai_client.embed_texts(["a", "b"]) == [None, None]


def test_network_failure_returns_none(fake_key, monkeypatch):
    monkeypatch.setattr(ai_client, "_post_json", lambda *a, **k: None)
    assert ai_client.classify_text("x") is None
    assert ai_client.embed_text("x") is None


# ---------------------------------------------------------------------------
# 分类解析
# ---------------------------------------------------------------------------


def test_classify_exact_category(fake_key, monkeypatch):
    monkeypatch.setattr(
        ai_client,
        "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "代码"}}]},
    )
    assert ai_client.classify_text("def main(): pass") == "代码"


def test_classify_fallback_substring(fake_key, monkeypatch):
    """模型多输出废话时，按包含关系兜底提取。"""
    monkeypatch.setattr(
        ai_client,
        "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "这段属于：代码。"}}]},
    )
    assert ai_client.classify_text("def main(): pass") == "代码"


def test_classify_unknown_answer_returns_none(fake_key, monkeypatch):
    monkeypatch.setattr(
        ai_client,
        "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "完全无关的词"}}]},
    )
    assert ai_client.classify_text("x") is None


def test_classify_malformed_response(fake_key, monkeypatch):
    monkeypatch.setattr(ai_client, "_post_json", lambda *a, **k: {"unexpected": True})
    assert ai_client.classify_text("x") is None


# ---------------------------------------------------------------------------
# 向量解析
# ---------------------------------------------------------------------------


def test_embed_texts_sorted_by_index(fake_key, monkeypatch):
    payload = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    monkeypatch.setattr(ai_client, "_post_json", lambda *a, **k: payload)
    assert ai_client.embed_texts(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_embed_texts_length_mismatch(fake_key, monkeypatch):
    monkeypatch.setattr(
        ai_client, "_post_json", lambda *a, **k: {"data": [{"index": 0, "embedding": [1.0]}]}
    )
    assert ai_client.embed_texts(["a", "b"]) == [None, None]


# ---------------------------------------------------------------------------
# 相似度与序列化
# ---------------------------------------------------------------------------


def test_cosine_similarity_basic():
    assert ai_client.cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert ai_client.cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert ai_client.cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)


def test_cosine_similarity_edge_cases():
    assert ai_client.cosine_similarity([0, 0], [1, 1]) == 0.0  # 零向量
    assert ai_client.cosine_similarity([1], [1, 2]) == 0.0  # 维度不一致


# ---------------------------------------------------------------------------
# 后台队列
# ---------------------------------------------------------------------------


def test_enqueue_analysis_skipped_when_not_configured(monkeypatch):
    monkeypatch.delenv(ai_client.ENV_PREFIX + "API_KEY", raising=False)
    # 先排空其它用例可能残留的任务，保证断言干净
    while True:
        try:
            ai_client._job_queue.get_nowait()
            ai_client._job_queue.task_done()
        except queue.Empty:
            break
    ai_client.enqueue_analysis(1, "文本")
    assert ai_client.queue_size() == 0  # 未配置：不入队


def test_enqueue_analysis_runs_worker_and_writes_db(fake_key, monkeypatch):
    """配置 AI 时：入队 -> 守护线程消费 -> 分类与向量写回数据库。"""
    item_id = storage.insert_item("text", content_hash="ai-1", text_content="def f(): pass")

    monkeypatch.setattr(ai_client, "classify_text", lambda text: "代码")
    monkeypatch.setattr(ai_client, "embed_text", lambda text: [1.0, 0.0])
    written: list[tuple] = []
    monkeypatch.setattr(storage, "set_category", lambda i, c: written.append(("cat", i, c)))
    monkeypatch.setattr(
        storage, "upsert_vector", lambda i, v, m: written.append(("vec", i, tuple(v), m))
    )

    ai_client.enqueue_analysis(item_id, "def f(): pass")
    ai_client._job_queue.join()  # 等待守护线程处理完

    assert ("cat", item_id, "代码") in written
    assert ("vec", item_id, (1.0, 0.0), ai_client.get_embed_model()) in written
