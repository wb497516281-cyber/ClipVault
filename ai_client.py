"""ai_client.py — 可选 AI 能力：文本分类 + 语义向量化 + 语义检索辅助。

依赖：仅 Python 标准库（urllib/json/math），不引入任何第三方 HTTP 库。

设计原则（对应项目硬性约束）：
  1. **完全可选**：未配置 API Key（或显式 CLIPVAULT_AI_ENABLED=0）时，
     所有函数安全降级 —— 分类返回 None、向量返回 None、语义检索不参与，
     核心功能（采集/存储/关键词搜索）零影响；
  2. **API Key 只走环境变量**：从 os.environ 读取，绝不硬编码；
     通过 OpenAI 兼容接口调用，换服务商只需改 CLIPVAULT_AI_BASE_URL；
  3. **不阻塞采集**：分析任务丢进后台队列，由守护线程处理；
     失败只记日志，绝不让 watcher / API 崩溃；
  4. 这里的「向量」是文本语义向量，与「图片不存 BLOB」的约束无关：
     图片原图与缩略图仍然只存文件路径，数据库 BLOB 仅用于 float32 文本向量。
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence

import config
import storage

logger = logging.getLogger("clipvault.ai")

# ---------------------------------------------------------------------------
# AI 配置项（全部可选；key 缺失即视为未配置）
#
# 读取走 config.get_setting：界面设置（settings.json）> 环境变量/.env > 默认值。
# 用户在 GUI「AI 设置」窗口里改的值保存到 settings.json，立即生效。
# ---------------------------------------------------------------------------

ENV_PREFIX = "CLIPVAULT_AI_"

#: 兼容 OpenAI 的接口地址（换成任何兼容服务商即可）
API_BASE = "https://api.openai.com/v1"

#: 分类模型
CHAT_MODEL = "gpt-4o-mini"

#: 向量模型
EMBED_MODEL = "text-embedding-3-small"

#: 候选分类（可用 CLIPVAULT_AI_CATEGORIES 覆盖，逗号分隔）
DEFAULT_CATEGORIES = ["链接", "代码", "命令", "邮箱电话", "地址", "账号凭证", "笔记", "其他"]

#: 主流厂商预设：选中后自动填 Base URL 与默认模型（embed 为空 = 该厂商无向量接口）
PROVIDERS: dict[str, dict[str, str]] = {
    "OpenAI": {
        "base": "https://api.openai.com/v1",
        "chat": "gpt-4o-mini",
        "embed": "text-embedding-3-small",
    },
    "DeepSeek": {
        "base": "https://api.deepseek.com/v1",
        "chat": "deepseek-chat",
        "embed": "",
    },
    "通义千问": {
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "chat": "qwen-plus",
        "embed": "text-embedding-v3",
    },
    "智谱 GLM": {
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "chat": "glm-4-flash",
        "embed": "embedding-2",
    },
    "Moonshot Kimi": {
        "base": "https://api.moonshot.cn/v1",
        "chat": "moonshot-v1-8k",
        "embed": "",
    },
    "硅基流动 SiliconFlow": {
        "base": "https://api.siliconflow.cn/v1",
        "chat": "Qwen/Qwen2.5-7B-Instruct",
        "embed": "BAAI/bge-m3",
    },
    "Ollama 本地": {
        "base": "http://localhost:11434/v1",
        "chat": "qwen2.5",
        "embed": "nomic-embed-text",
    },
    "自定义": {"base": "", "chat": "", "embed": ""},
}

#: 单次请求超时（秒）
REQUEST_TIMEOUT = 10.0


def _get_setting(name: str, default: str) -> str:
    """读取 CLIPVAULT_AI_* 配置（界面设置 > 环境变量 > 默认值）。"""
    return config.get_setting(ENV_PREFIX + name, default)


def get_api_key() -> str:
    """API Key：来自界面设置或环境变量 / .env，绝不硬编码进代码。"""
    return config.get_setting(ENV_PREFIX + "API_KEY")


def is_enabled() -> bool:
    """AI 是否显式关闭：CLIPVAULT_AI_ENABLED=0 时强制关闭。"""
    return _get_setting("ENABLED", "1").lower() not in ("0", "false", "no")


def is_configured() -> bool:
    """是否具备调用条件：已配置 Key 且未被显式关闭。"""
    return bool(get_api_key()) and is_enabled()


def get_categories() -> list[str]:
    """候选分类列表。"""
    raw = _get_setting("CATEGORIES", "")
    if raw:
        items = [c.strip() for c in raw.split(",") if c.strip()]
        if items:
            return items
    return list(DEFAULT_CATEGORIES)


def get_timeout() -> float:
    """请求超时。"""
    try:
        return float(_get_setting("TIMEOUT", str(REQUEST_TIMEOUT)))
    except ValueError:
        return REQUEST_TIMEOUT


def get_chat_model() -> str:
    """分类所用模型名。"""
    return _get_setting("CHAT_MODEL", CHAT_MODEL)


def get_embed_model() -> str:
    """向量所用模型名。"""
    return _get_setting("EMBED_MODEL", EMBED_MODEL)


# ---------------------------------------------------------------------------
# HTTP 调用（OpenAI 兼容协议）
# ---------------------------------------------------------------------------


def _post_json(path: str, payload: dict) -> dict | None:
    """POST JSON 到 AI 接口；任何网络/协议错误都只记日志并返回 None。"""
    key = get_api_key()
    if not key:
        return None
    url = _get_setting("BASE_URL", API_BASE).rstrip("/") + path
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=get_timeout()) as response:
            return json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("AI 请求失败（%s）：%s", url, exc)
        return None


def _get_json(path: str) -> dict | None:
    """GET JSON（OpenAI 兼容接口）；配了 Key 自动带鉴权，失败只记日志返回 None。"""
    base = _get_setting("BASE_URL", API_BASE).strip().rstrip("/")
    if not base:
        return None
    url = base + path
    headers = {"Content-Type": "application/json"}
    key = get_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=get_timeout()) as response:
            return json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("AI 请求失败（%s）：%s", url, exc)
        return None


def fetch_models() -> list[str]:
    """拉取当前接口的可用模型列表（GET /models），按 ID 排序去重。

    用户在设置窗口填好 URL 和 Key 后点「拉取模型」，即可把模型列表
    填进分类/向量模型下拉框。失败（网络错/Key 错/接口不兼容）返回空列表。
    """
    data = _get_json("/models")
    if not data:
        return []
    rows = data.get("data")
    if not isinstance(rows, list):
        return []
    ids = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
    return sorted(dict.fromkeys(ids))


# ---------------------------------------------------------------------------
# 能力一：文本分类
# ---------------------------------------------------------------------------


def classify_text(text: str) -> str | None:
    """把文本分类到候选类别之一；未配置/失败返回 None。

    只取模型输出的第一个候选分类名（容错解析：整段返回里包含候选词即算命中）。
    """
    if not is_configured() or not text.strip():
        return None
    categories = get_categories()
    prompt = (
        "你是剪贴板内容分类器。请判断下面这段剪贴板内容属于哪个分类，"
        f"只能从这些分类里选一个：{'、'.join(categories)}。"
        "只输出分类名本身，不要输出任何解释、标点或多余文字。"
    )
    data = _post_json(
        "/chat/completions",
        {
            "model": _get_setting("CHAT_MODEL", CHAT_MODEL),
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text[:1000]},
            ],
            "temperature": 0,
            "max_tokens": 16,
        },
    )
    if not data:
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    content = content.strip()
    # 精确命中优先；否则按包含关系兜底（模型偶尔会多输出废话）
    for category in categories:
        if content == category:
            return category
    for category in categories:
        if category in content:
            return category
    return None


# ---------------------------------------------------------------------------
# 能力二：语义向量化
# ---------------------------------------------------------------------------


def embed_text(text: str) -> list[float] | None:
    """把文本转成向量；未配置/失败返回 None。"""
    if not is_configured() or not text.strip():
        return None
    results = embed_texts([text])
    return results[0] if results else None


def embed_texts(texts: Sequence[str]) -> list[list[float] | None]:
    """批量向量化；按输入顺序返回，失败位置为 None。"""
    if not is_configured() or not texts:
        return [None] * len(texts)
    data = _post_json(
        "/embeddings",
        {
            "model": _get_setting("EMBED_MODEL", EMBED_MODEL),
            "input": list(texts),
        },
    )
    if not data:
        return [None] * len(texts)
    try:
        rows = sorted(data["data"], key=lambda r: r.get("index", 0))
        embeddings = [r["embedding"] for r in rows]
    except (KeyError, TypeError):
        return [None] * len(texts)
    if len(embeddings) != len(texts):
        return [None] * len(texts)
    return embeddings


# ---------------------------------------------------------------------------
# 能力三：相似度计算（供语义检索使用）
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；任一零向量返回 0.0（不抛异常）。"""
    if len(a) != len(b) or not a:
        return 0.0
    # 上面已校验等长，strict=True 让契约显式化（长度不一致直接抛错而非静默截断）
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# 后台分析队列：分类 + 向量化，守护线程处理，失败只记日志
# ---------------------------------------------------------------------------

#: 任务队列：(item_id, text_content)；上限 1000，满了直接丢弃（个人工具规模足够）
_job_queue: queue.Queue = queue.Queue(maxsize=1000)

_worker_lock = threading.Lock()
_worker_started = False


def _worker_loop() -> None:
    """队列消费循环：对每条文本依次做分类与向量化，结果写回数据库。"""
    while True:
        item_id, text = _job_queue.get()
        try:
            category = classify_text(text)
            if category:
                storage.set_category(item_id, category)
            vector = embed_text(text)
            if vector:
                storage.upsert_vector(item_id, vector, _get_setting("EMBED_MODEL", EMBED_MODEL))
        except Exception as exc:  # 兜底：绝不让守护线程退出
            logger.warning("AI 分析任务失败（id=%s）：%s", item_id, exc)
        finally:
            _job_queue.task_done()


def _ensure_worker() -> None:
    """懒启动守护线程（首次入队时启动，整个进程只启动一次）。"""
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker_loop, name="clipvault-ai-worker", daemon=True).start()
            _worker_started = True


def enqueue_analysis(item_id: int, text: str) -> None:
    """把某条文本的分析（分类 + 向量化）任务入队；AI 未配置时直接跳过。"""
    if not is_configured() or not text.strip():
        return
    _ensure_worker()
    try:
        _job_queue.put_nowait((item_id, text))
    except queue.Full:
        logger.info("AI 分析队列已满，丢弃 id=%s", item_id)


def enqueue_reindex(rows: Sequence[tuple[int, str]]) -> int:
    """批量入队（供托盘菜单「立即补建语义向量」使用）；返回实际入队条数。"""
    if not is_configured():
        return 0
    count = 0
    for item_id, text in rows:
        if text and text.strip():
            enqueue_analysis(item_id, text)
            count += 1
    return count


def queue_size() -> int:
    """当前排队中的任务数。"""
    return _job_queue.qsize()
