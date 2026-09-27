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


def test_local_base_needs_no_key(monkeypatch):
    """Ollama 等本机服务免 Key：base 是 localhost 且未配 Key 也算已配置。"""
    monkeypatch.delenv(ai_client.ENV_PREFIX + "API_KEY", raising=False)
    monkeypatch.setattr(
        ai_client,
        "_get_setting",
        lambda name, default: "http://localhost:11434/v1" if name == "BASE_URL" else default,
    )
    assert ai_client.is_configured() is True


def test_timeout_falls_back_when_not_positive(monkeypatch):
    """TIMEOUT 设为 0/负数/非法时回落默认，避免「AI 永远连不上」。"""
    monkeypatch.setattr(
        ai_client, "_get_setting", lambda name, default: "0" if name == "TIMEOUT" else default
    )
    assert ai_client.get_timeout() == ai_client.REQUEST_TIMEOUT
    monkeypatch.setattr(
        ai_client, "_get_setting", lambda name, default: "abc" if name == "TIMEOUT" else default
    )
    assert ai_client.get_timeout() == ai_client.REQUEST_TIMEOUT
    monkeypatch.setattr(
        ai_client, "_get_setting", lambda name, default: "3.5" if name == "TIMEOUT" else default
    )
    assert ai_client.get_timeout() == 3.5


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
# 厂商预设与模型拉取
# ---------------------------------------------------------------------------


def test_providers_presets_are_complete():
    """每个厂商预设都必须带 base/chat/embed 三个键，base 是 http(s) 地址。"""
    assert "自定义" in ai_client.PROVIDERS
    for name, preset in ai_client.PROVIDERS.items():
        assert set(preset.keys()) == {"base", "chat", "embed"}, name
        if preset["base"]:
            assert preset["base"].startswith(("http://", "https://")), name
    for name in ("OpenAI", "DeepSeek", "通义千问", "智谱 GLM", "Moonshot Kimi", "Ollama 本地"):
        assert name in ai_client.PROVIDERS, name


def test_fetch_models_success(fake_key, monkeypatch):
    """拉取成功：返回排序去重的模型 ID 列表。"""
    monkeypatch.setattr(
        ai_client,
        "_get_json",
        lambda path: {
            "data": [
                {"id": "gpt-4o-mini"},
                {"id": "text-embedding-3-small"},
                {"id": "gpt-4o-mini"},  # 重复应去重
            ]
        },
    )
    assert ai_client.fetch_models() == ["gpt-4o-mini", "text-embedding-3-small"]


def test_fetch_models_failure_returns_empty(fake_key, monkeypatch):
    """拉取失败（网络/Key 错/接口不兼容）返回空列表，不抛异常。"""
    monkeypatch.setattr(ai_client, "_get_json", lambda path: None)
    assert ai_client.fetch_models() == []


def test_fetch_models_malformed_payload(fake_key, monkeypatch):
    """接口返回结构不对时返回空列表。"""
    monkeypatch.setattr(ai_client, "_get_json", lambda path: {"unexpected": True})
    assert ai_client.fetch_models() == []
    monkeypatch.setattr(ai_client, "_get_json", lambda path: {"data": "not-a-list"})
    assert ai_client.fetch_models() == []


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


def test_worker_skips_stale_tasks(fake_key, monkeypatch):
    """陈旧任务：入队后内容被编辑过，worker 不得把旧文本的结果写回。"""
    import threading

    item_id = storage.insert_item("text", content_hash="ai-2", text_content="旧内容")

    # 门控保证顺序确定：worker 卡在 classify 里等门，测试改完内容再放行
    gate = threading.Event()
    worker_in = threading.Event()

    def _slow_classify(text):
        worker_in.set()
        gate.wait(5)
        return "代码"

    monkeypatch.setattr(ai_client, "classify_text", _slow_classify)
    monkeypatch.setattr(ai_client, "embed_text", lambda text: [1.0, 0.0])
    written: list[tuple] = []
    monkeypatch.setattr(storage, "set_category", lambda i, c: written.append(("cat", i, c)))
    monkeypatch.setattr(
        storage, "upsert_vector", lambda i, v, m: written.append(("vec", i, tuple(v), m))
    )

    ai_client.enqueue_analysis(item_id, "旧内容")
    assert worker_in.wait(5)  # worker 已取出任务并卡在 classify
    # 模拟用户在 worker 处理前编辑了该条（内容变、向量清、分类清）
    storage.update_item_content(item_id, "新内容")
    gate.set()  # 放行 worker 继续
    ai_client._job_queue.join()

    assert written == []  # 旧文本的分类/向量都没有写回


# ---------------------------------------------------------------------------
# 连接自检（设置窗「测试连接」）
# ---------------------------------------------------------------------------


def test_connection_not_configured(monkeypatch):
    """未配置：直接报未配置，不发请求。"""
    monkeypatch.delenv(ai_client.ENV_PREFIX + "API_KEY", raising=False)
    ok, message = ai_client.test_connection()
    assert ok is False
    assert "尚未配置" in message


def test_connection_tests_embed_when_model_configured(fake_key, monkeypatch):
    """配了向量模型：测嵌入接口，成功信息带维度与模型名。"""
    monkeypatch.setattr(
        ai_client,
        "_post_json",
        lambda *a, **k: {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
    )
    ok, message = ai_client.test_connection()
    assert ok is True
    assert "维度 3" in message


def test_connection_falls_back_to_chat_without_embed_model(fake_key, monkeypatch):
    """没配向量模型（DeepSeek/Moonshot）：测对话接口——回归「永远连接失败」误报。"""
    monkeypatch.setattr(
        ai_client,
        "_get_setting",
        lambda name, default: "" if name == "EMBED_MODEL" else default,
    )
    monkeypatch.setattr(
        ai_client, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "pong"}}]}
    )
    ok, message = ai_client.test_connection()
    assert ok is True
    assert "对话接口" in message


def test_connection_chat_failure_reports_reason(fake_key, monkeypatch):
    """对话接口也失败时给出失败信息（不抛异常）。"""
    monkeypatch.setattr(
        ai_client,
        "_get_setting",
        lambda name, default: "" if name == "EMBED_MODEL" else default,
    )
    monkeypatch.setattr(ai_client, "_post_json", lambda *a, **k: None)
    ok, message = ai_client.test_connection()
    assert ok is False
    assert "对话接口" in message


def test_connection_failure_includes_http_status(fake_key, monkeypatch):
    """HTTP 错误（如 401 Key 错）要带进失败信息，用户能自己排查。"""
    import urllib.error

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.example.com/v1/embeddings", 401, "Unauthorized", {}, None
        )

    monkeypatch.setattr(ai_client.urllib.request, "urlopen", boom)
    ok, message = ai_client.test_connection()
    assert ok is False
    assert "401" in message
    assert ai_client.last_error() is not None
