"""storage.py — SQLite 存储层：初始化、建表、插入、按内容哈希去重查询。

依赖：仅 Python 3.12 标准库（sqlite3 / pathlib / datetime / struct），无第三方依赖。
设计约定：
  1. 图片绝不存 BLOB 进数据库，只保存本地文件的相对路径；
  2. 所有路径统一用 pathlib.Path 管理，不用字符串拼路径；
  3. content_type 区分 'text' 与 'image'，两类内容共用一张表；
  4. content_hash 为 SHA-256 内容哈希，库内加 UNIQUE 约束兜底去重；
  5. clip_vectors 表里的 BLOB 是「文本语义向量」(float32)，不是图片数据，
     图片仍然只存路径 —— 两者互不影响；
  6. 分组（clip_groups + clip_group_members）是多对多关系：一条记录可以同时
     属于多个分组；删除分组只删成员关系，绝不删除条目本身。
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import get_data_dir, load_env_file, migrate_legacy_data_dir

# 导入时先加载 .env（保证 CLIPVAULT_DATA_DIR 等变量先生效）
load_env_file()

# ---------------------------------------------------------------------------
# 路径定义（以本文件位置为基准，切换工作目录也不会错）
# ---------------------------------------------------------------------------

# 数据目录：CLIPVAULT_DATA_DIR 优先，默认 clipboard_data/（运行时自动创建）
DATA_DIR: Path = get_data_dir()

#: SQLite 数据库文件：clipboard_data/clipboard.db
DB_PATH: Path = DATA_DIR / "clipboard.db"

#: 图片目录：clipboard_data/images/（原图与缩略图都放这里）
IMAGE_DIR: Path = DATA_DIR / "images"

# ---------------------------------------------------------------------------
# 建表语句
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS clipboard_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type    TEXT    NOT NULL,              -- 'text' 或 'image'
    text_content    TEXT,                          -- 文本内容（图片行为 NULL）
    image_path      TEXT,                          -- 原图相对路径（文本行为 NULL）
    thumbnail_path  TEXT,                          -- 缩略图相对路径（文本行为 NULL）
    content_hash    TEXT    NOT NULL UNIQUE,       -- SHA-256 内容哈希，去重依据
    source_app      TEXT,                          -- 来源应用（复制时的前台窗口标题）
    created_at      TEXT    NOT NULL,              -- 本地时间，格式 'YYYY-MM-DD HH:MM:SS'
    is_pinned       INTEGER NOT NULL DEFAULT 0,    -- 是否置顶：0=否，1=是
    pinned_at       TEXT,                          -- 置顶时间；未置顶为 NULL
    category        TEXT                           -- AI 分类标签（未配置 AI 时为 NULL）
);
"""

#: 语义向量表：只对文本条目有意义；BLOB 存 float32 小端向量，不是图片数据
CREATE_VECTORS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS clip_vectors (
    item_id     INTEGER PRIMARY KEY,               -- 对应 clipboard_items.id
    model       TEXT    NOT NULL,                  -- 生成该向量所用的模型
    dim         INTEGER NOT NULL,                  -- 向量维度（校验用）
    vector      BLOB    NOT NULL,                  -- float32 小字节序向量
    updated_at  TEXT    NOT NULL
);
"""

#: 分组表：用户或 AI 创建的分组（名称唯一）
CREATE_GROUPS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS clip_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,           -- 分组名（唯一，重名直接报错）
    position    INTEGER NOT NULL DEFAULT 0,        -- 排序权重，越大越靠前
    created_at  TEXT    NOT NULL                   -- 本地时间，格式 'YYYY-MM-DD HH:MM:SS'
);
"""

#: 分组成员表：多对多（item_id 可同时属于多个 group_id）
CREATE_GROUP_MEMBERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS clip_group_members (
    group_id    INTEGER NOT NULL,                  -- 对应 clip_groups.id
    item_id     INTEGER NOT NULL,                  -- 对应 clipboard_items.id
    added_at    TEXT    NOT NULL,
    PRIMARY KEY (group_id, item_id)
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_clipboard_items_created_at
    ON clipboard_items(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clipboard_items_pin
    ON clipboard_items(is_pinned, pinned_at DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clipboard_items_category
    ON clipboard_items(category);
CREATE INDEX IF NOT EXISTS idx_clip_group_members_item
    ON clip_group_members(item_id);
"""


def _ensure_optional_columns(conn: sqlite3.Connection) -> None:
    """老库升级：补齐 is_pinned / pinned_at / category 三列。

    SQLite 的 ALTER TABLE ADD COLUMN 成本极低；配合 CREATE TABLE IF NOT EXISTS，
    新库直接带齐三列，老库启动时自动补齐。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clipboard_items)")}
    if "is_pinned" not in columns:
        conn.execute("ALTER TABLE clipboard_items ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0")
    if "pinned_at" not in columns:
        conn.execute("ALTER TABLE clipboard_items ADD COLUMN pinned_at TEXT")
    if "category" not in columns:
        conn.execute("ALTER TABLE clipboard_items ADD COLUMN category TEXT")
    if "title" not in columns:
        conn.execute("ALTER TABLE clipboard_items ADD COLUMN title TEXT")


# ---------------------------------------------------------------------------
# 初始化与连接
# ---------------------------------------------------------------------------


def init_db() -> None:
    """初始化数据库：创建目录、建表、建索引，并开启 WAL 模式。

    WAL 模式让「采集线程写、界面读取」并发时互不阻塞，24 小时常开更稳。
    先跑一次性迁移（打包版旧数据目录 -> %LOCALAPPDATA%/ClipVault/data），
    再建目录——保证任何入口（GUI/托盘/纯采集）首启都会搬数据。
    """
    migrate_legacy_data_dir()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            # 极少数并发初始化/文件系统不支持时会留在 delete 模式，功能不受影响但记一笔
            print(f"[提示] SQLite WAL 模式未生效（当前 {mode}），并发性能可能下降", flush=True)
        # 顺序很重要：先建表 -> 再补列（老库迁移）-> 最后建索引。
        # 索引里引用了 is_pinned 等后加的列，补列前建索引会在老库上报错。
        conn.executescript(
            CREATE_TABLE_SQL
            + CREATE_VECTORS_TABLE_SQL
            + CREATE_GROUPS_TABLE_SQL
            + CREATE_GROUP_MEMBERS_TABLE_SQL
        )
        _ensure_optional_columns(conn)  # 老库自动补齐置顶/分类/名称相关列
        conn.executescript(CREATE_INDEX_SQL)
        conn.commit()


def get_connection() -> sqlite3.Connection:
    """获取一个 SQLite 连接（行工厂为 sqlite3.Row）。

    采集线程与界面线程各自独立连接；WAL 模式下读写并发互不阻塞。
    调用方负责关闭（配合 contextlib.closing 使用）。
    """
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def to_relative(path: Path) -> str:
    """把绝对路径转成相对「数据目录」的 posix 字符串（数据库只存相对路径）。

    以 DATA_DIR 为基准而不是项目根：数据目录整体搬家
    （比如设置 CLIPVAULT_DATA_DIR）后，库里存的路径依然有效。
    典型值：images/thumb_20250101_120000_123456.png
    """
    try:
        return path.resolve().relative_to(DATA_DIR).as_posix()
    except ValueError:
        # 不在数据目录内的路径（理论上不会发生），退回原样
        return path.as_posix()


# ---------------------------------------------------------------------------
# 查询与插入
# ---------------------------------------------------------------------------


def find_by_hash(content_hash: str) -> dict[str, Any] | None:
    """按内容哈希查询剪贴板记录；命中返回字典，未命中返回 None（去重查询）。"""
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM clipboard_items WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone()
    return dict(row) if row is not None else None


def insert_item(
    content_type: str,
    content_hash: str,
    text_content: str | None = None,
    image_path: str | None = None,
    thumbnail_path: str | None = None,
    source_app: str | None = None,
) -> int | None:
    """插入一条剪贴板记录。

    返回新行的 id；如果 content_hash 已存在（重复内容）则返回 None，不重复写入。
    """
    # 先查一次做常规去重（同时把 UNIQUE 约束作为兜底）
    if find_by_hash(content_hash) is not None:
        return None
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with closing(get_connection()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO clipboard_items
                    (content_type, text_content, image_path, thumbnail_path,
                     content_hash, source_app, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_type,
                    text_content,
                    image_path,
                    thumbnail_path,
                    content_hash,
                    source_app,
                    created_at,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        # UNIQUE 冲突，说明并发/异常场景下内容已存在
        return None


# ---------------------------------------------------------------------------
# AI 分类与语义向量（可选能力，未配置 AI 时这些接口不会被调用）
# ---------------------------------------------------------------------------


def set_category(item_id: int, category: str) -> None:
    """写入 AI 分类标签。"""
    with closing(get_connection()) as conn:
        conn.execute(
            "UPDATE clipboard_items SET category = ? WHERE id = ?",
            (category, item_id),
        )
        conn.commit()


def items_without_vector(limit: int = 200) -> list[dict[str, Any]]:
    """还没有语义向量的文本条目（供托盘「立即补建语义向量」使用）。"""
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.text_content
            FROM clipboard_items i
            LEFT JOIN clip_vectors v ON v.item_id = i.id
            WHERE i.content_type = 'text' AND v.item_id IS NULL
            ORDER BY i.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_vector(item_id: int, vector: Sequence[float], model: str) -> None:
    """写入/更新某条文本的语义向量（float32 小端 BLOB）。"""
    blob = struct.pack(f"<{len(vector)}f", *vector)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO clip_vectors (item_id, model, dim, vector, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                model = excluded.model,
                dim = excluded.dim,
                vector = excluded.vector,
                updated_at = excluded.updated_at
            """,
            (item_id, model, len(vector), blob, now),
        )
        conn.commit()


def load_vectors() -> list[tuple[int, tuple[float, ...]]]:
    """加载全部语义向量，返回 [(item_id, vector), ...]。

    注意：不按模型过滤 —— 如果用户更换了 CLIPVAULT_AI_EMBED_MODEL，
    旧向量仍是上一个模型生成的；个人工具规模下重新全量 reindex 即可刷新。
    """
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT item_id, vector FROM clip_vectors").fetchall()
    result: list[tuple[int, tuple[float, ...]]] = []
    for row in rows:
        blob = row["vector"]
        count = len(blob) // 4
        result.append((row["item_id"], struct.unpack(f"<{count}f", blob[: count * 4])))
    return result


def stats() -> dict[str, int]:
    """统计信息（供 GUI 标题栏与 AI 状态显示）。"""
    with closing(get_connection()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM clipboard_items").fetchone()[0]
        categorized = conn.execute(
            "SELECT COUNT(*) FROM clipboard_items WHERE category IS NOT NULL"
        ).fetchone()[0]
        embedded = conn.execute("SELECT COUNT(*) FROM clip_vectors").fetchone()[0]
        text_total = conn.execute(
            "SELECT COUNT(*) FROM clipboard_items WHERE content_type = 'text'"
        ).fetchone()[0]
    return {
        "total": total,
        "text_total": text_total,
        "categorized": categorized,
        "embedded": embedded,
        "pending_vectors": max(0, text_total - embedded),
    }


# ---------------------------------------------------------------------------
# 列表与关键词搜索（供原生 GUI 使用）
# ---------------------------------------------------------------------------


def escape_like(value: str) -> str:
    """转义 LIKE 通配符，防止搜索词里的 % 和 _ 被当成通配符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_items(
    q: str | None = None,
    limit: int = 200,
    content_type: str | None = None,
    group_id: int | None = None,
    grouped: str = "all",
) -> list[dict[str, Any]]:
    """按展示顺序返回条目：置顶优先，其次按时间倒序。

    q 非空时对 text_content / title（自定义命名）/ source_app 做 LIKE 关键词搜索；
    content_type 可传 'text' / 'image' 做类型过滤；
    group_id 非空时只返回该分组的成员；
    grouped 可传 'ungrouped'（只要无分组成员）/ 'grouped'（只要有分组成员）/ 'all'。
    返回字段与数据库行一致（含 image_path，仅供内部文件定位用）。
    """
    sql = "SELECT DISTINCT i.* FROM clipboard_items i"
    params: list[Any] = []
    conditions: list[str] = []
    if group_id is not None:
        sql += " JOIN clip_group_members m ON m.item_id = i.id AND m.group_id = ?"
        params.append(group_id)
    if grouped == "ungrouped":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM clip_group_members gm WHERE gm.item_id = i.id)"
        )
    elif grouped == "grouped":
        conditions.append(
            "EXISTS (SELECT 1 FROM clip_group_members gm WHERE gm.item_id = i.id)"
        )
    if q:
        # 搜索范围：内容 + 自定义命名 + 来源应用。
        # 命名是手动分组/编辑时起的名字（title），必须参与搜索 —— 否则
        # 「搜得到内容、搜不到自己起的名」会让人以为条目丢了。
        conditions.append(
            "(i.text_content LIKE ? ESCAPE '\\' OR i.title LIKE ? ESCAPE '\\'"
            " OR i.source_app LIKE ? ESCAPE '\\')"
        )
        like = f"%{escape_like(q)}%"
        params.extend([like, like, like])
    if content_type in ("text", "image"):
        conditions.append("i.content_type = ?")
        params.append(content_type)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += (
        " ORDER BY i.is_pinned DESC, COALESCE(i.pinned_at, '') DESC, i.created_at DESC, i.id DESC"
        " LIMIT ?"
    )
    params.append(limit)
    with closing(get_connection()) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def find_by_ids(ids: list[int]) -> dict[int, dict[str, Any]]:
    """按 id 批量取条目，返回 {id: 行字典}（供语义检索结果保序取回）。"""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with closing(get_connection()) as conn:
        rows = conn.execute(
            f"SELECT * FROM clipboard_items WHERE id IN ({placeholders})", tuple(ids)
        ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def get_item(item_id: int) -> dict[str, Any] | None:
    """按 id 取单条；不存在返回 None。"""
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT * FROM clipboard_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row is not None else None


def update_pin(item_id: int, pinned: bool) -> None:
    """置顶 / 取消置顶（pinned_at 在置顶时记录时间，用于同级排序）。"""
    pinned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if pinned else None
    with closing(get_connection()) as conn:
        conn.execute(
            "UPDATE clipboard_items SET is_pinned = ?, pinned_at = ? WHERE id = ?",
            (1 if pinned else 0, pinned_at, item_id),
        )
        conn.commit()


def delete_item(item_id: int) -> list[str]:
    """删除条目及其语义向量，返回该条的图片相对路径列表（供调用方删文件）。

    文件删除由调用方负责（GUI 层掌握路径解析与 IMAGE_DIR 校验）。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT image_path, thumbnail_path FROM clipboard_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return []
        paths = [p for p in (row["image_path"], row["thumbnail_path"]) if p]
        conn.execute("DELETE FROM clipboard_items WHERE id = ?", (item_id,))
        conn.execute("DELETE FROM clip_vectors WHERE item_id = ?", (item_id,))
        conn.commit()
    return paths


def update_item_title(item_id: int, title: str) -> None:
    """给条目命名（title）；传空串表示清除名称。"""
    with closing(get_connection()) as conn:
        conn.execute(
            "UPDATE clipboard_items SET title = ? WHERE id = ?",
            (title.strip() or None, item_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 分组：clip_groups + clip_group_members（多对多，删分组不删条目）
# ---------------------------------------------------------------------------


def _now() -> str:
    """本地时间字符串，格式 'YYYY-MM-DD HH:MM:SS'。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_position(conn: sqlite3.Connection) -> int:
    """新建分组的排序权重：当前最大值 + 1（新分组排最前）。"""
    row = conn.execute("SELECT COALESCE(MAX(position), 0) FROM clip_groups").fetchone()
    return int(row[0]) + 1


def create_group(name: str) -> int:
    """新建分组，返回分组 id；名称为空或重名抛 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("分组名不能为空")
    if len(name) > 30:
        name = name[:30]
    try:
        with closing(get_connection()) as conn:
            with conn:  # 单事务：插入失败自动回滚
                cursor = conn.execute(
                    "INSERT INTO clip_groups (name, position, created_at) VALUES (?, ?, ?)",
                    (name, _next_position(conn), _now()),
                )
    except sqlite3.IntegrityError as exc:
        # UNIQUE 冲突：同名分组已存在
        raise ValueError("分组名已存在") from exc
    return int(cursor.lastrowid)


def rename_group(group_id: int, new_name: str) -> None:
    """重命名分组；名称为空 / 与别的分组重名抛 ValueError。"""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("分组名不能为空")
    if len(new_name) > 30:
        new_name = new_name[:30]
    try:
        with closing(get_connection()) as conn:
            with conn:
                conn.execute(
                    "UPDATE clip_groups SET name = ? WHERE id = ?", (new_name, group_id)
                )
    except sqlite3.IntegrityError as exc:
        raise ValueError("分组名已存在") from exc


def delete_group(group_id: int) -> None:
    """删除分组：只删分组行与成员关系，条目本身一律保留。"""
    with closing(get_connection()) as conn:
        with conn:  # 单事务：分组行与成员关系要么全删要么都不删
            conn.execute("DELETE FROM clip_group_members WHERE group_id = ?", (group_id,))
            conn.execute("DELETE FROM clip_groups WHERE id = ?", (group_id,))


def get_group(group_id: int) -> dict[str, Any] | None:
    """按 id 取单个分组（含成员数）；不存在返回 None。"""
    with closing(get_connection()) as conn:
        row = conn.execute(
            """
            SELECT g.id, g.name, g.position, g.created_at,
                   (SELECT COUNT(*) FROM clip_group_members m WHERE m.group_id = g.id) AS count
            FROM clip_groups g WHERE g.id = ?
            """,
            (group_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_groups() -> list[dict[str, Any]]:
    """全部分组（按 position 倒序 → 创建时间倒序），每个分组带成员计数。"""
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT g.id, g.name, g.position, g.created_at, COUNT(m.item_id) AS count
            FROM clip_groups g
            LEFT JOIN clip_group_members m ON m.group_id = g.id
            GROUP BY g.id
            ORDER BY g.position DESC, g.created_at DESC, g.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def group_overview() -> dict[str, Any]:
    """分组总览（供 GUI 侧边栏）：总条数、未分组数、分组列表（含计数）。"""
    with closing(get_connection()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM clipboard_items").fetchone()[0]
        ungrouped = conn.execute(
            """
            SELECT COUNT(*) FROM clipboard_items i
            WHERE NOT EXISTS (
                SELECT 1 FROM clip_group_members m WHERE m.item_id = i.id
            )
            """
        ).fetchone()[0]
    return {"total": int(total), "ungrouped": int(ungrouped), "groups": list_groups()}


def item_group_ids(item_id: int) -> list[int]:
    """某条目所属的全部分组 id（按分组排序）。"""
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT m.group_id
            FROM clip_group_members m
            JOIN clip_groups g ON g.id = m.group_id
            WHERE m.item_id = ?
            ORDER BY g.position DESC, g.created_at DESC, g.id DESC
            """,
            (item_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def group_names_by_ids(ids: Sequence[int]) -> dict[int, list[str]]:
    """批量取条目对应的分组名（供卡片显示分组徽章）。"""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with closing(get_connection()) as conn:
        rows = conn.execute(
            f"""
            SELECT m.item_id, g.name
            FROM clip_group_members m
            JOIN clip_groups g ON g.id = m.group_id
            WHERE m.item_id IN ({placeholders})
            ORDER BY g.position DESC, g.created_at DESC, g.id DESC
            """,
            tuple(ids),
        ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(int(row["item_id"]), []).append(row["name"])
    return result


def add_item_to_group(item_id: int, group_id: int) -> bool:
    """把条目加入分组；返回 True=新加入，False=本来就在组里。"""
    with closing(get_connection()) as conn:
        with conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO clip_group_members (group_id, item_id, added_at)"
                " VALUES (?, ?, ?)",
                (group_id, item_id, _now()),
            )
            return cursor.rowcount > 0


def remove_item_from_group(item_id: int, group_id: int) -> bool:
    """把条目移出分组；返回 True=确实移出过，False=本来就不在组里。"""
    with closing(get_connection()) as conn:
        with conn:
            cursor = conn.execute(
                "DELETE FROM clip_group_members WHERE group_id = ? AND item_id = ?",
                (group_id, item_id),
            )
            return cursor.rowcount > 0


def set_item_groups(item_id: int, group_ids: Sequence[int]) -> None:
    """整体设置条目的分组（替换语义）：不在 group_ids 里的组一律移出。"""
    wanted = [int(gid) for gid in group_ids]
    with closing(get_connection()) as conn:
        with conn:  # 单事务：清空 + 重写，避免中间态
            if wanted:
                placeholders = ",".join("?" * len(wanted))
                existing = {
                    int(row[0])
                    for row in conn.execute(
                        f"SELECT id FROM clip_groups WHERE id IN ({placeholders})", tuple(wanted)
                    )
                }
            else:
                existing = set()
            conn.execute("DELETE FROM clip_group_members WHERE item_id = ?", (item_id,))
            for gid in sorted(existing):
                conn.execute(
                    "INSERT INTO clip_group_members (group_id, item_id, added_at) VALUES (?, ?, ?)",
                    (gid, item_id, _now()),
                )


def ungrouped_text_items(limit: int = 500) -> list[dict[str, Any]]:
    """还没有任何分组的文本条目（供「AI 自动分组」使用，新的在前）。"""
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.text_content
            FROM clipboard_items i
            WHERE i.content_type = 'text'
              AND NOT EXISTS (
                  SELECT 1 FROM clip_group_members m WHERE m.item_id = i.id
              )
            ORDER BY i.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_ungrouped_items(older_than_days: int | None = None) -> tuple[int, list[str]]:
    """批量清理「未分组」条目：删行 + 删向量，返回 (删除条数, 图片相对路径列表)。

    older_than_days 非空时只删「保存超过 N 天」的——按每条自己的 created_at
    单独算年龄（不是攒到每周统一清一批）；None 时删全部未分组（手动清理用）。
    入了组的条目永不删（分组是「收藏夹」，是保留信号）。
    图片文件由调用方按相对路径删除（本函数只动数据库）。
    """
    cutoff: str | None = None
    if older_than_days is not None:
        cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_connection()) as conn:
        with conn:  # 单事务：行与向量要么全删要么都不删
            sql = """
                SELECT i.id, i.image_path, i.thumbnail_path
                FROM clipboard_items i
                WHERE NOT EXISTS (
                    SELECT 1 FROM clip_group_members m WHERE m.item_id = i.id
                )
            """
            params: list[Any] = []
            if cutoff is not None:
                sql += " AND i.created_at <= ?"
                params.append(cutoff)
            rows = conn.execute(sql, tuple(params)).fetchall()
            if not rows:
                return 0, []
            ids = [int(row["id"]) for row in rows]
            paths = [
                p for row in rows for p in (row["image_path"], row["thumbnail_path"]) if p
            ]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM clip_vectors WHERE item_id IN ({placeholders})", tuple(ids)
            )
            conn.execute(
                f"DELETE FROM clipboard_items WHERE id IN ({placeholders})", tuple(ids)
            )
    return len(ids), paths


class ContentConflictError(Exception):
    """修改后的内容与另一条条目重复（内容哈希冲突）。"""


def update_item_content(item_id: int, new_text: str) -> None:
    """修改文本条目的内容（内容哈希同步重算，分类与旧向量失效）。

    - 与其它条目内容撞车时抛 ContentConflictError，不做修改；
    - 内容为空抛 ValueError；
    - 对图片条目不存在的 id 调用抛 ValueError（避免 GUI 误报成功）。
    冲突检查 + 内容更新 + 向量清理在同一事务内完成，杜绝中间态。
    """
    new_text = new_text.strip()
    if not new_text:
        raise ValueError("内容不能为空")
    digest = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    try:
        with closing(get_connection()) as conn:
            with conn:  # 单事务：异常自动回滚
                row = conn.execute(
                    "SELECT content_type FROM clipboard_items WHERE id = ?", (item_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"条目 {item_id} 不存在")
                if row["content_type"] != "text":
                    raise ValueError("图片条目不支持修改内容")
                # 检查-写入之间有并发插入同内容行的可能：UNIQUE 约束兜底
                conn.execute(
                    "UPDATE clipboard_items SET text_content = ?, content_hash = ?,"
                    " category = NULL WHERE id = ?",
                    (new_text, digest, item_id),
                )
                # 内容变了旧向量失效：一并清掉，等 AI 队列重建
                conn.execute("DELETE FROM clip_vectors WHERE item_id = ?", (item_id,))
    except sqlite3.IntegrityError as exc:
        # UNIQUE 冲突：并发场景下另一条已占用该内容
        raise ContentConflictError("修改后的内容与另一条记录重复") from exc
