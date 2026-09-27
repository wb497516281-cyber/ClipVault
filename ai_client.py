"""ai_client.py — 可选 AI 能力：文本分类 + 语义向量化 + 智能分组 + 语义检索辅助。

依赖：仅 Python 标准库（urllib/json/math），不引入任何第三方 HTTP 库。

设计原则（对应项目硬性约束）：
  1. **完全可选**：未配置 API Key（或显式 CLIPVAULT_AI_ENABLED=0）时，
     所有函数安全降级 —— 分类返回 None、向量返回 None、分组返回空 dict、
     语义检索不参与，核心功能（采集/存储/关键词搜索）零影响；
  2. **API Key 只走环境变量**：从 os.environ 读取，绝不硬编码；
     通过 OpenAI 兼容接口调用，换服务商只需改 CLIPVAULT_AI_BASE_URL；
  3. **不阻塞采集**：分析任务丢进后台队列，由守护线程处理；
     失败只记日志，绝不让 watcher / API 崩溃；
  4. 这里的「向量」是文本语义向量，与「图片不存 BLOB」的约束无关：
     图片原图与缩略图仍然只存文件路径，数据库 BLOB 仅用于 float32 文本向量。
  5. **智能分组**只依赖分类模型（chat），只用文本摘要，不发送整篇内容；
     输出按 JSON 数组约定，解析层做围栏/废话/字段缺失的容错。
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

#: 最近一次请求失败的底层原因（供「连接测试」展示，纯诊断用）
_last_error: str | None = None


def _note_error(exc: Exception) -> None:
    """记录最近一次失败的简短原因（HTTP 码 / 异常摘要），供连接自检展示。"""
    global _last_error
    text = str(exc).strip() or type(exc).__name__
    _last_error = text[:120]


def last_error() -> str | None:
    """最近一次请求失败的原因（没有则 None）。"""
    return _last_error


def _get_setting(name: str, default: str) -> str:
    """读取 CLIPVAULT_AI_* 配置（界面设置 > 环境变量 > 默认值）。"""
    return config.get_setting(ENV_PREFIX + name, default)


def get_api_key() -> str:
    """API Key：来自界面设置或环境变量 / .env，绝不硬编码进代码。"""
    return config.get_setting(ENV_PREFIX + "API_KEY")


def is_enabled() -> bool:
    """AI 是否显式关闭：CLIPVAULT_AI_ENABLED=0 时强制关闭。"""
    return _get_setting("ENABLED", "1").lower() not in ("0", "false", "no")


def _is_local_base() -> bool:
    """当前接口是否本机地址（Ollama 等本地服务无需 API Key）。"""
    base = _get_setting("BASE_URL", API_BASE).strip().lower()
    return base.startswith(("http://localhost", "http://127.0.0.1"))


def is_configured() -> bool:
    """是否具备调用条件：已配置 Key（或本机服务免 Key）且未被显式关闭。"""
    if not is_enabled():
        return False
    return bool(get_api_key()) or _is_local_base()


def get_categories() -> list[str]:
    """候选分类列表。"""
    raw = _get_setting("CATEGORIES", "")
    if raw:
        items = [c.strip() for c in raw.split(",") if c.strip()]
        if items:
            return items
    return list(DEFAULT_CATEGORIES)


def get_timeout() -> float:
    """请求超时（非正数/非法值回落默认，避免 urlopen 进入非阻塞）。"""
    try:
        value = float(_get_setting("TIMEOUT", str(REQUEST_TIMEOUT)))
    except ValueError:
        return REQUEST_TIMEOUT
    return value if value > 0 else REQUEST_TIMEOUT


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
    if not key and not _is_local_base():
        return None
    url = _get_setting("BASE_URL", API_BASE).rstrip("/") + path
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=get_timeout()) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("AI 请求失败（%s）：%s", url, exc)
        _note_error(exc)
        return None
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
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("AI 请求失败（%s）：%s", url, exc)
        _note_error(exc)
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
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("AI 请求失败（%s）：%s", url, exc)
        _note_error(exc)
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
# 连接自检（设置窗「测试连接」）：按当前实际配置探测，而不是无脑测向量
#
# DeepSeek / Moonshot 等厂商没有 embedding 接口，旧实现只测 /embeddings，
# 导致「Key 和对话接口明明好的」也永远显示连接失败。现在：
#   配了向量模型 -> 测 /embeddings（返回维度）；
#   没配向量模型 -> 测 /chat/completions（最小请求，回复任意内容即算通）。
# 失败时带上底层原因（HTTP 码/异常摘要），方便用户自己排查。
# ---------------------------------------------------------------------------


def _chat_ping() -> str | None:
    """最小对话请求：用来验证 chat 接口与 Key 是否可用。"""
    data = _post_json(
        "/chat/completions",
        {
            "model": _get_setting("CHAT_MODEL", CHAT_MODEL),
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        },
    )
    if not data:
        return None
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return None


def test_connection() -> tuple[bool, str]:
    """连接自检：返回 (是否成功, 人类可读信息)。

    - 未配置（无 Key / 显式关闭）：直接报未配置；
    - 配了向量模型：测嵌入接口（语义搜索依赖它）；
    - 没配向量模型（DeepSeek/Moonshot 常见）：测对话接口
      （自动分类与 AI 分组本来就只用 chat，测它才是用户真正需要的）。
    """
    global _last_error
    if not is_configured():
        return False, "尚未配置 API Key 或 AI 已停用"
    try:
        embed_model = get_embed_model().strip()
        chat_model = get_chat_model().strip()
        if embed_model:
            _last_error = None
            vector = embed_text("连接测试")
            if vector:
                return True, f"连接成功！向量接口可用（{embed_model}，维度 {len(vector)}）"
            return False, f"向量接口连接失败：{_last_error or '接口未返回向量'}"
        if chat_model:
            _last_error = None
            if _chat_ping() is not None:
                return True, f"连接成功！对话接口可用（{chat_model}）"
            return False, f"对话接口连接失败：{_last_error or '接口未返回内容'}"
        return False, "尚未配置分类模型或向量模型"
    except Exception as exc:  # 兜底：自检本身绝不让设置窗崩掉
        return False, f"连接出错：{exc}"


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
# 能力二：智能分组（批量分配到现有分组 / 按需新建分组）
#
# 只用分类模型（chat），不依赖向量接口：DeepSeek、Moonshot 这类没有 embedding
# 的厂商也能用。输出约定为 JSON 数组 [{"id": 1, "group": "分组名"}, ...]，
# 解析层对 markdown 围栏、前后废话、字段缺失一律容错。
# ---------------------------------------------------------------------------

#: AI 自动分组的每批条数：单批过大容易触发超时或输出被 max_tokens 截断
GROUP_BATCH_SIZE = 40

#: 单批允许新建的最大分组数（防止模型发疯一样造一堆分组）
GROUP_MAX_NEW_PER_BATCH = 8


def _preview_for_grouping(text: str, max_chars: int = 120) -> str:
    """内容摘要：换行压平 + 截断，减少 token 消耗。"""
    flat = " ".join((text or "").split())
    return flat[:max_chars]


def parse_group_assignment(
    content: str,
    valid_ids: set[int] | None = None,
    existing_groups: Sequence[str] = (),
    max_new_groups: int = GROUP_MAX_NEW_PER_BATCH,
) -> dict[int, str]:
    """把模型输出解析成 {item_id: group_name}。

    容错点（模型经常不听话）：
      - ```json 代码围栏 / JSON 前后带解释文字：截取首个 '[' 或 '{' 到末个 ']'/'}'；
      - 输出为 {"1": "分组"} 字典形态：按 id -> group 处理；
      - 数组元素缺 id / group 字段、group 非字符串：跳过该条；
      - id 不在 valid_ids（本次提交的条目）：忽略，绝不张冠李戴；
      - 分组名超长或带引号/标点：清理后仍超长则跳过；
      - 不在 existing_groups 里的「新分组名」超过 max_new_groups 时，
        多出的分配丢弃（宁可少分也不错分）。
    """
    if not isinstance(content, str) or not content.strip():
        return {}
    existing_set = {g.strip() for g in existing_groups if g and g.strip()}
    text = content.strip()
    # 去掉 markdown 代码围栏（```json ... ```）
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:] if len(lines) > 1 else []
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    starts = [pos for pos in (text.find("["), text.find("{")) if pos >= 0]
    if not starts:
        return {}
    start = min(starts)
    end = max(text.rfind("]"), text.rfind("}"))
    if end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}

    pairs: list[tuple[int, str]] = []
    if isinstance(data, dict):
        # {"1": "工作", "2": "代码"} 形态
        for key, value in data.items():
            try:
                pairs.append((int(key), value if isinstance(value, str) else ""))
            except (TypeError, ValueError):
                continue
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                item_id = int(entry.get("id"))  # type: ignore[arg-type]
            except (KeyError, TypeError, ValueError):
                continue
            name = entry.get("group") or entry.get("name") or entry.get("分组")
            pairs.append((item_id, name if isinstance(name, str) else ""))
    else:
        return {}

    result: dict[int, str] = {}
    new_count = 0
    for item_id, raw_name in pairs:
        if valid_ids is not None and item_id not in valid_ids:
            continue
        name = raw_name.strip().strip("\"'`。；;，, ")
        if not name or len(name) > 30:
            continue
        if name in existing_set or name in result.values():
            result[item_id] = name  # 复用现有/已收分组名，不占新建额度
            continue
        new_count += 1
        if new_count > max_new_groups:
            continue
        result[item_id] = name
    return result


def assign_groups(
    rows: Sequence[tuple[int, str]],
    existing_groups: Sequence[str] = (),
) -> dict[int, str]:
    """把一批条目分配给分组；rows 为 [(item_id, 内容摘要), ...]。

    - 优先复用 existing_groups 里的同名分组，不合适才新建；
    - 每 GROUP_BATCH_SIZE 条一批，顺序发送，后一批能复用前一批新建的分组名；
    - 未配置 AI / 网络失败 / 解析失败时该批整体跳过，返回已拿到的部分结果；
    - 绝不对不存在的 item_id 造结果（valid_ids 门控）。
    """
    if not is_configured() or not rows:
        return {}
    existing = [g.strip() for g in existing_groups if g and g.strip()]
    result: dict[int, str] = {}
    for start in range(0, len(rows), GROUP_BATCH_SIZE):
        batch = rows[start : start + GROUP_BATCH_SIZE]
        valid_ids = {item_id for item_id, _ in batch}
        existing_text = "、".join(dict.fromkeys(existing)) if existing else "（暂无，可自行新建）"
        payload = {
            "model": _get_setting("CHAT_MODEL", CHAT_MODEL),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是剪贴板内容分组助手。下面给你 {n} 条剪贴板记录，每条带编号和内容摘要。\n"
                        "请为每条记录选一个最合适的分组：\n"
                        f"1. 优先复用现有分组：{existing_text}；全都明显不合适时才新建；\n"
                        f"2. 新分组名用简短中文（2~6 个字），本批新建不超过 {GROUP_MAX_NEW_PER_BATCH} 个；\n"
                        "3. 每条必须且只能归入一个分组，编号必须原样保留；\n"
                        "4. 只输出 JSON 数组，不要解释、不要代码围栏：\n"
                        '[{{"id": 1, "group": "分组名"}}, ...]'
                    ).format(n=len(batch)),
                },
                {
                    "role": "user",
                    "content": "\n".join(
                        f"{item_id}. {_preview_for_grouping(text or '')}" for item_id, text in batch
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": max(128, len(batch) * 24),
        }
        data = _post_json("/chat/completions", payload)
        if not data:
            logger.info("AI 分组批次失败（第 %d 批，共 %d 条）", start, len(batch))
            continue
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            continue
        assigned = parse_group_assignment(content, valid_ids, existing)
        if assigned:
            result.update(assigned)
            # 后一批沿用本批新建的分组名，避免同内容分裂成两个组
            existing.extend(dict.fromkeys(assigned.values()))
    return result


def suggest_group(text: str, existing_groups: Sequence[str] = ()) -> str | None:
    """单条内容的分组建议（复用现有分组优先）；未配置/失败返回 None。"""
    if not is_configured() or not (text or "").strip():
        return None
    result = assign_groups([(0, text)], list(existing_groups))
    return result.get(0)


# ---------------------------------------------------------------------------
# 能力三：语义向量化
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
    """队列消费循环：对每条文本依次做分类与向量化，结果写回数据库。

    陈旧校验放在「网络请求之后、写库之前」：用户在请求期间编辑该条
    （内容变、向量已清）时丢弃结果，禁止把旧文本的向量/分类写回去
    造成语义搜索错配。
    """
    while True:
        item_id, text = _job_queue.get()
        try:
            category = classify_text(text)
            vector = embed_text(text)
            # 写回前最后校验一次内容（编辑常发生在网络请求耗时期间）
            current = storage.get_item(item_id)
            if current is None or (current.get("text_content") or "") != text:
                logger.info("AI 任务过期已丢弃（id=%s，内容已被修改或删除）", item_id)
                continue
            if category:
                storage.set_category(item_id, category)
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
