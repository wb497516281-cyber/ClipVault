"""cleanup.py — 历史上限清理：未分组内容按「每条保存满 N 天」自动删，分组内容永久保留。

策略（用户明确要求）：
  1. **逐条算年龄**：一条未分组条目从它自己的保存时间起算，满 7 天才删；
     不是「攒到每周统一清一批」——今天复制的内容哪怕明天就到「清理日」也不会动；
  2. 入了组的条目永不自动删除——分组是「收藏夹」，是保留信号；
  3. 置顶但未分组的一并删：置顶只是展示优先级，不是保留信号。

触发方式：
  - 自动：GUI 刷新链路调用 maybe_run()（进程内每小时最多真跑一次），
    把「未分组且 created_at 早于 N 天前」的条目删掉；删过就 toast 汇报；
  - 手动：分组栏「🧹 清理未分组」按钮，立即删**全部**未分组（不看作没看过天数），
    二次确认并展示条数。

状态记录：<数据目录>/cleanup_state.json（记录上次运行时间与删除条数，仅诊断用）。
  不放 settings.json——那个文件由 AI 设置窗整体覆写，会被冲掉。
  纯采集模式（clipvault-watch）没有 GUI 刷新链路，不跑清理。

保留天数可用环境变量 CLIPVAULT_CLEANUP_DAYS 覆盖（默认 7）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config
import storage

logger = logging.getLogger("clipvault.cleanup")

#: 未分组内容保留天数：每条按自己的保存时间单独算
DEFAULT_CLEANUP_DAYS = 7

#: 进程内两次真跑的最小间隔（秒）：每小时一次，SQL 廉价但没必要 5 秒跑一趟
RUN_THROTTLE_SECONDS = 3600.0

#: 上次真跑的单调时钟（进程内节流）
_last_run_monotonic: float | None = None


def _now_str() -> str:
    """本地时间字符串，格式 'YYYY-MM-DD HH:MM:SS'。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def state_path() -> Path:
    """状态文件路径：<数据目录>/cleanup_state.json。

    动态取数据目录（而不是 import 期缓存）：测试换 CLIPVAULT_DATA_DIR 后要生效。
    """
    return config.get_data_dir() / "cleanup_state.json"


def load_state() -> dict[str, Any]:
    """读取清理状态；文件不存在/损坏时返回空字典（容错）。"""
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(deleted: int) -> None:
    """写入清理状态（先写临时文件再原子替换，防止崩溃写半截）。"""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(
            {"last_run": _now_str(), "last_deleted": deleted},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def get_cleanup_days() -> int:
    """保留天数：CLIPVAULT_CLEANUP_DAYS 覆盖，非法值回落默认 7。"""
    raw = os.environ.get("CLIPVAULT_CLEANUP_DAYS", "").strip()
    if not raw:
        return DEFAULT_CLEANUP_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_CLEANUP_DAYS
    return days if days > 0 else DEFAULT_CLEANUP_DAYS


def rule_text() -> str:
    """清理规则的人类可读文案（供界面展示）。"""
    days = get_cleanup_days()
    return f"自动规则：未分组满 {days} 天按每条保存时间单独删，分组内容永久保留"


def _delete_image_files(paths: list[str]) -> None:
    """按相对路径删除图片文件；只删数据目录 images/ 内的文件，防路径穿越。"""
    for rel in paths:
        path = (storage.DATA_DIR / rel).resolve()
        if path.is_file() and storage.IMAGE_DIR in path.parents:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("清理图片文件失败（%s）：%s", rel, exc)


def _execute(older_than_days: int | None) -> dict[str, Any]:
    """执行删除并更新状态；异常不外抛（清理失败绝不让刷新链路崩掉）。"""
    try:
        count, paths = storage.delete_ungrouped_items(older_than_days=older_than_days)
        _delete_image_files(paths)
    except Exception as exc:
        logger.warning("清理未分组条目失败：%s", exc)
        return {"deleted": 0, "ran": False, "reason": f"清理失败：{exc}"}
    save_state(count)
    return {"deleted": count, "ran": True, "reason": ""}


def run_auto() -> dict[str, Any]:
    """自动清理：删「未分组且保存超过 N 天」的条目（逐条算年龄）。"""
    return _execute(get_cleanup_days())


def run_manual() -> dict[str, Any]:
    """手动清理：立即删全部未分组条目（不看作没看过天数）。"""
    return _execute(None)


def maybe_run() -> dict[str, None] | None:
    """GUI 刷新链路入口：进程内每小时最多真跑一次，跑了才返回结果。

    删了东西返回 {"deleted": N}（调用方按 N>0 刷新界面 + toast）；
    没删/被节流都返回 None。
    """
    global _last_run_monotonic
    now_mono = time.monotonic()
    if (
        _last_run_monotonic is not None
        and now_mono - _last_run_monotonic < RUN_THROTTLE_SECONDS
    ):
        return None
    _last_run_monotonic = now_mono
    result = run_auto()
    return {"deleted": result["deleted"]} if result["deleted"] > 0 else None
