"""rules.py — 确定性分组规则：命中规则的条目自动入组。

规则设计：**规则本身是确定性的**（命中必入指定分组），**检测手段可以用 AI**。
当前内置一条规则：

  API 密钥检测：AI 判断内容是否为 API 密钥 / Token / 访问凭证，
  命中则入「API」组（不存在则自动创建）。

触发方式：
  1. 新内容实时：AI 分析 worker 处理完分类/向量后调用 apply_rule()；
  2. 手动批量：分组栏「🛡 规则分组」按钮，对未分组条目跑一遍（后台线程）。

安全边界：
  - 只处理**未分组**条目——用户手动/AI 分过组的是明确意图，规则不碰；
  - 检测拿不准（模型答非所问/请求失败/未配置 AI）一律不分组，宁可漏检；
  - 规则执行异常只记日志，绝不影响采集/搜索主链路。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import ai_client
import storage

logger = logging.getLogger("clipvault.rules")

#: API 规则的目标分组名（不存在时自动创建）
API_RULE_GROUP = "API"


def _ensure_group(name: str) -> int | None:
    """按名字找分组，没有就创建；返回分组 id（失败返回 None）。"""
    for group in storage.list_groups():
        if group["name"] == name:
            return group["id"]
    try:
        return storage.create_group(name)
    except ValueError:
        # 并发/重名兜底：再查一次
        for group in storage.list_groups():
            if group["name"] == name:
                return group["id"]
        return None


def apply_rule(item_id: int, text: str) -> bool:
    """对单条内容跑规则；命中并入组返回 True，否则 False。

    只处理未分组条目；AI 未配置/检测失败都安全返回 False。
    """
    if not (text or "").strip():
        return False
    if storage.item_group_ids(item_id):
        return False  # 已有分组：尊重既有意图
    try:
        if not ai_client.detect_api_key(text):
            return False
    except Exception as exc:
        logger.info("API 规则检测失败（id=%s）：%s", item_id, exc)
        return False
    group_id = _ensure_group(API_RULE_GROUP)
    if group_id is None:
        return False
    storage.add_item_to_group(item_id, group_id)
    return True


def run_rules(rows: Sequence[tuple[int, str]]) -> dict[str, Any]:
    """批量跑规则；rows 为 [(item_id, text), ...]。

    返回 {"applied": 入组条数, "total": 处理条数}。逐条独立：某条失败不影响其他。
    """
    applied = 0
    for item_id, text in rows:
        try:
            if apply_rule(item_id, text):
                applied += 1
        except Exception as exc:  # 兜底：单条异常不中断整批
            logger.info("规则分组单条失败（id=%s）：%s", item_id, exc)
    return {"applied": applied, "total": len(rows)}


def rule_text() -> str:
    """规则说明的人类可读文案（供界面展示）。"""
    return f"规则：检测到 API 密钥/Token → 自动入「{API_RULE_GROUP}」组（只处理未分组条目）"
