"""config.py — 运行配置：.env 文件加载与路径基准。

依赖：仅 Python 标准库。

为什么需要它：
  1. 让 CLIPVAULT_DATA_DIR、CLIPVAULT_AI_API_KEY 等环境变量既能从系统环境读取，
     也能写在项目根目录的 .env 文件里（不引入 python-dotenv 依赖）；
  2. 统一 BASE_DIR 基准，所有路径都用 pathlib.Path 管理。

约定：
  - .env 只在对应环境变量尚未设置时生效，系统环境变量优先级更高；
  - 源码运行时 BASE_DIR = 项目根目录；PyInstaller 打包后（sys.frozen）
    BASE_DIR = exe 所在目录，方便用户把 .env 放在 exe 旁边；
  - 数据目录（数据库/图片/设置）：CLIPVAULT_DATA_DIR > 打包版
    %LOCALAPPDATA%/ClipVault/data > 源码版 <项目根>/clipboard_data。
    打包版数据必须放 exe 外面——躺在安装目录里，重建/覆盖更新会连数据
    一起删（真实踩过坑，见 migrate_legacy_data_dir）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

#: 应用版本号（与 pyproject.toml 的 version 保持一致，tests/test_config.py 有校验）
APP_VERSION = "1.4.0"


def _runtime_base_dir() -> Path:
    """运行基准目录：源码模式为项目根；打包（frozen）模式为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


#: 运行基准目录（clipvault 项目根，或打包后 exe 所在目录）
BASE_DIR: Path = _runtime_base_dir()

#: 环境变量文件路径
ENV_FILE: Path = BASE_DIR / ".env"


def _strip_inline_comment(value: str) -> str:
    """去掉行内注释（KEY=value  # 备注）；引号包裹的值原样返回。"""
    if value[:1] in ('"', "'"):
        # 引号值：找到收尾引号为止，其余丢弃
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    hash_pos = value.find(" #")
    if hash_pos >= 0:
        value = value[:hash_pos]
    return value.strip()


def load_env_file(path: Path | None = None) -> None:
    """把 .env 文件中的 KEY=VALUE 载入 os.environ。

    规则：
      - 不覆盖已存在的环境变量（系统环境优先）；
      - 忽略空行、# 注释行与不含 = 的行；
      - 兼容 export 前缀、成对引号、行内注释；
      - 用 utf-8-sig 读取，兼容记事本另存的带 BOM 文件。
    """
    env_path = path if path is not None else ENV_FILE
    if not env_path.is_file():
        return
    try:
        # utf-8-sig 兼容 BOM；errors="replace" 防止 GBK 等非 UTF-8 文件让进程起不来
        text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, UnicodeDecodeError):
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):  # 兼容 shell 风格
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        # 只剥「成对」引号；不对称引号（如 'key）保持原样
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        value = _strip_inline_comment(value)
        if value:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# 环境变量读取辅助（集中管理，避免各处散落写 os.environ）
# ---------------------------------------------------------------------------


def get_data_dir() -> Path:
    """数据目录：CLIPVAULT_DATA_DIR 优先，默认按运行模式决定。

    - 打包（frozen）模式：%LOCALAPPDATA%/ClipVault/data —— exe 外面，
      重建/覆盖更新都不会碰它；没有 LOCALAPPDATA 时退回用户目录下；
    - 源码模式：<项目根>/clipboard_data（开发便利，tests 用临时目录覆盖）。
    """
    raw = os.environ.get("CLIPVAULT_DATA_DIR", "").strip()
    if raw:
        return Path(raw).resolve()
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "ClipVault" / "data"
    return BASE_DIR / "clipboard_data"


def migrate_legacy_data_dir() -> None:
    """打包版一次性迁移：exe 旁的旧 clipboard_data -> %LOCALAPPDATA%/ClipVault/data。

    历史包袱：旧版本把数据放在 exe 旁边（dist/ClipVault/clipboard_data），
    重新打包/覆盖安装会连数据一起删掉。新版默认 %LOCALAPPDATA%/ClipVault/data，
    首次启动把旧目录整体搬过去（新目录已存在则不动，绝不覆盖用户现有数据）。

    仅在满足以下全部条件时执行：打包模式、没有显式 CLIPVAULT_DATA_DIR、
    旧目录存在、新目录不存在。任何异常只警告不阻断启动。
    """
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("CLIPVAULT_DATA_DIR", "").strip():
        return  # 用户显式指定了位置，不搬
    legacy = BASE_DIR / "clipboard_data"
    target = get_data_dir()
    if not legacy.is_dir() or target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        print(f"[迁移] 旧数据目录已搬到 {target}", flush=True)
    except OSError as exc:
        # 跨盘/权限问题：不阻断启动，新位置会建空库，旧数据仍在原处
        print(f"[警告] 数据目录迁移失败（{exc}），继续使用 {target}", flush=True)


# ---------------------------------------------------------------------------
# GUI 设置文件（settings.json）：界面上的配置保存于此
#
# 优先级：settings.json（界面设置） > 环境变量 / .env（高级用户） > 内置默认值。
# 界面是用户最近一次显式操作，理应当场生效；需要环境变量覆盖的场景见 README。
# 文件位于数据目录内（随数据目录走，已被 .gitignore）。
# ---------------------------------------------------------------------------

#: 设置缓存：{路径: (mtime, 数据)}，按 mtime 失效，外部改动也能感知
_settings_cache: dict[str, tuple[float | None, dict]] = {}


def settings_path() -> Path:
    """设置文件路径：<数据目录>/settings.json。"""
    return get_data_dir() / "settings.json"


def _invalidate_settings_cache() -> None:
    """清空设置缓存（保存 / 清除后立即调用，避免 mtime 精度问题）。"""
    _settings_cache.clear()


def load_settings() -> dict[str, str]:
    """读取 settings.json；文件不存在或损坏时返回空字典（容错，不抛异常）。

    返回缓存 dict 的副本，避免调用方不慎修改污染缓存。
    """
    path = settings_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _settings_cache.pop(str(path), None)
        return {}
    cached = _settings_cache.get(str(path))
    if cached is not None and cached[0] == mtime:
        return dict(cached[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _settings_cache[str(path)] = (mtime, data)
    return dict(data)


def save_settings(values: dict[str, str]) -> None:
    """把界面设置写入 settings.json（自动建目录），并刷新缓存。

    先写临时文件再 os.replace 原子替换：崩溃/断电时不会留下半截文件
    （否则 load_settings 会静默返回空 -> 用户以为配好的 Key 悄悄失效）。
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    _invalidate_settings_cache()


def clear_settings() -> None:
    """删除 settings.json（恢复「环境变量 + 默认值」行为）。"""
    settings_path().unlink(missing_ok=True)
    _invalidate_settings_cache()


def get_setting(key: str, default: str = "") -> str:
    """统一读取配置项：settings.json > 环境变量/.env > 默认值。"""
    value = load_settings().get(key)
    if value is not None and str(value).strip():
        return str(value).strip()
    return os.environ.get(key, "").strip() or default
