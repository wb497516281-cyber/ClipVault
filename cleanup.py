"""cleanup.py — 历史上限清理：未分组内容每周自动清理，分组内容永久保留。

策略（用户明确要求）：
  1. 未分组的条目每周清一次（删行 + 删向量 + 删图片文件）；
  2. 入了组的条目永不自动删除——分组是「收藏夹」，是保留信号；
  3. 置顶但未分组的一并删：置顶只是展示优先级，不是保留信号。

触发方式：
  - 自动：GUI 刷新链路调用 maybe_run()（进程内每小时最多查一次状态文件），
    距上次清理 >= CLEANUP_DAYS 天即执行；执行后 toast 汇报删了多少；
  - 手动：分组栏「🧹 清理未分组」按钮，Confirm 后立即执行；
  - 升级首启：只登记 last_cleanup（不立即删），7 天后才第一次真扫，
    避免用户一升级就看到老历史被清空。

状态记录：<数据目录>/cleanup_state.json（{"last_cleanup": ..., "last_deleted": N}）。
  不放 settings.json——那个文件由 AI 设置窗整体覆写，会被冲掉。
  纯采集模式（clipvault-watch）没有 GUI 刷新链路，不跑清理。

间隔可用环境变量 CLIPVAULT_CLEANUP_DAYS 覆盖（默认 7）。
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

#: 默认清理间隔（天）：每周一次
DEFAULT_CLEANUP_DAYS = 7

#: 进程内两次到期检查的最小间隔（秒）：每小时一次，状态文件读取足够廉价
CHECK_THROTTLE_SECONDS = 3600.0

#: 上次到期检查的单调时钟（进程内节流）
_last_check_monotonic: float | None = None


def _now_str() -> str:
    """本地时间字符串，格式 'YYYY-MM-DD HH:MM:SS'。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def state_path() -> Path:
    """状态文件路径：<数据目录>/cleanup_state.json。

    动态取数据目录（而不是 import 期缓存）：测试换 CLIPVAULT_DATA_DIR 后要生效。
    """
    return config.get_data_dir() / "cleanup_state.json"


def load_state() -> dict[str, Any]:
    """读取清理状态；文件不存在/损坏时返回空字典（容错，视为从未清理）。"""
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(last_cleanup: str, last_deleted: int) -> None:
    """写入清理状态（先写临时文件再原子替换，防止崩溃写半截）。"""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(
            {"last_cleanup": last_cleanup, "last_deleted": last_deleted},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def get_cleanup_days() -> int:
    """清理间隔天数：CLIPVAULT_CLEANUP_DAYS 覆盖，非法值回落默认 7。"""
    raw = os.environ.get("CLIPVAULT_CLEANUP_DAYS", "").strip()
    if not raw:
        return DEFAULT_CLEANUP_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_CLEANUP_DAYS
    return days if days > 0 else DEFAULT_CLEANUP_DAYS


def last_cleanup_time() -> datetime | None:
    """上次清理时间；从未清理过返回 None。"""
    raw = load_state().get("last_cleanup")
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def is_due(now: datetime | None = None) -> bool:
    """是否到了该清理的时候：从未清理过视为到期（由 ensure_state 兜底登记）。"""
    last = last_cleanup_time()
    if last is None:
        return True
    now = now or datetime.now()
    return (now - last).total_seconds() >= get_cleanup_days() * 86400


def next_cleanup_text() -> str:
    """下次自动清理时间的人类可读文案（供界面展示）。"""
    last = last_cleanup_time()
    if last is None:
        return "下次自动清理：升级满 7 天后"
    from datetime import timedelta

    nxt = last + timedelta(days=get_cleanup_days())
    return f"下次自动清理：{nxt:%Y-%m-%d %H:%M}"


def ensure_state() -> None:
    """升级首启兜底：没有状态文件就登记当前时间（7 天后才第一次真删）。"""
    if last_cleanup_time() is None:
        save_state(_now_str(), 0)


def _delete_image_files(paths: list[str]) -> None:
    """按相对路径删除图片文件；只删数据目录 images/ 内的文件，防路径穿越。"""
    for rel in paths:
        path = (storage.DATA_DIR / rel).resolve()
        if path.is_file() and storage.IMAGE_DIR in path.parents:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("清理图片文件失败（%s）：%s", rel, exc)


def run_cleanup(force: bool = False) -> dict[str, Any]:
    """执行一次清理；返回 {"deleted": N, "ran": bool, "reason": str}。

    force=False 时未到期直接跳过（ran=False）；到期/强制都真正删除并更新状态。
    """
    if not force and not is_due():
        return {"deleted": 0, "ran": False, "reason": "未到清理周期"}
    try:
        count, paths = storage.delete_ungrouped_items()
        _delete_image_files(paths)
    except Exception as exc:  # 清理失败绝不让刷新链路崩掉
        logger.warning("清理未分组条目失败：%s", exc)
        return {"deleted": 0, "ran": False, "reason": f"清理失败：{exc}"}
    # 失败也更新时间：避免数据库异常时每小时重试造成日志刷屏
    save_state(_now_str(), count)
    return {"deleted": count, "ran": True, "reason": ""}


def maybe_run() -> dict[str, Any] | None:
    """GUI 刷新链路入口：到期才清理，没到期/刚查过都返回 None。

    进程内节流到每小时一次真实判断；返回非 None 表示真的跑了一次清理
    （调用方按 deleted > 0 决定是否刷新界面与 toast）。
    """
    global _last_check_monotonic
    now_mono = time.monotonic()
    if (
        _last_check_monotonic is not None
        and now_mono - _last_check_monotonic < CHECK_THROTTLE_SECONDS
    ):
        return None
    _last_check_monotonic = now_mono
    ensure_state()  # 状态文件被外部删掉时重新登记
    if not is_due():
        return None
    return run_cleanup(force=True)
