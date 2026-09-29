"""updater.py — 自动更新：启动时检查 GitHub Release，后台下载，重启热替换。

设计原则：
  1. **只对打包版有意义**：源码/开发模式（sys.frozen 为 False）一律不检查，
     开发者用 git 管理代码，避免开发环境被 release 覆盖；
  2. **零依赖**：只用标准库 urllib，走系统代理（urllib 默认读环境变量/注册表代理）；
  3. **不阻塞启动**：检查与下载都在守护线程里完成，结果回主线程提示；
  4. **不碰用户数据**：更新 zip 只含程序文件；数据在 %LOCALAPPDATA%/ClipVault/data，
     覆盖安装动不到它（这正是数据目录搬出安装目录的收益）；
  5. **失败静默**：网络不通/接口限流就当没这回事，下次启动再试；
     手动「检查更新」才会把失败原因提示出来。

流程：
  check_for_update() -> 有新版本 -> download_update(zip) 存临时目录
  -> 用户确认 -> install_update(zip)：生成 .cmd（等本进程退出 ->
  Expand-Archive 覆盖安装目录 -> 重启 ClipVault.exe -> 自清理）-> 应用退出。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import config

logger = logging.getLogger("clipvault.updater")

#: GitHub 仓库（owner/repo），release 与资产都从这里取
GITHUB_REPO = "wb497516281-cyber/ClipVault"

#: 最新 release 查询接口（/latest 不含草稿与预发布）
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

#: 检查/下载超时（秒）：启动链路里不能卡
REQUEST_TIMEOUT = 10.0

#: 下载缓存放临时目录：%TEMP%/ClipVault-update
CACHE_DIR = Path(tempfile.gettempdir()) / "ClipVault-update"


def current_version() -> str:
    """当前版本号（config.APP_VERSION，与 pyproject 同步）。"""
    return config.APP_VERSION


def parse_version(tag: str) -> tuple[int, ...] | None:
    """把 'v1.2.0' / '1.2.0-beta' 解析成 (1, 2, 0)；非数字段/空值返回 None。"""
    cleaned = (tag or "").strip().lstrip("vV")
    # 截掉预发布/构建后缀（-beta、+build 等），只比较数字主段
    cleaned = cleaned.replace("+", "-").split("-", 1)[0]
    parts = cleaned.split(".")
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        numbers.append(int(part))
    return tuple(numbers) if numbers else None


def is_newer(remote_tag: str, current: str | None = None) -> bool:
    """远端 tag 是否比当前版本新（版本段逐位比，长的更大；1.2.0 < 1.2.1 < 1.3.0）。"""
    remote = parse_version(remote_tag)
    local = parse_version(current if current is not None else current_version())
    if remote is None or local is None:
        return False
    # 补齐短版再比：1.2 vs 1.2.0 视为相等
    length = max(len(remote), len(local))
    remote += (0,) * (length - len(remote))
    local += (0,) * (length - len(local))
    return remote > local


def _get_json(url: str, timeout: float = REQUEST_TIMEOUT) -> dict | None:
    """GET JSON；任何网络/协议错误只记日志返回 None（启动链路绝不抛异常）。"""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ClipVault/{current_version()}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.info("更新检查请求失败（%s）：%s", url, exc)
        return None


def _pick_zip_url(release: dict) -> str | None:
    """从 release 资产里挑第一个 .zip（发布包约定为 ClipVault-vX.Y.Z-win64.zip）。"""
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        url = str(asset.get("browser_download_url") or "")
        name = str(asset.get("name") or "")
        if url and name.lower().endswith(".zip"):
            return url
    return None


def check_for_update() -> dict | None:
    """检查是否有新版本；返回 {"version","name","body","zip_url","html_url"} 或 None。

    源码模式直接返回 None（只打包版需要自更新）。
    """
    if not getattr(sys, "frozen", False):
        return None
    release = _get_json(API_LATEST)
    if not release or release.get("draft") or release.get("prerelease"):
        return None
    tag = str(release.get("tag_name") or "")
    if not tag or not is_newer(tag):
        return None
    zip_url = _pick_zip_url(release)
    if not zip_url:
        logger.info("最新 release %s 没有 zip 资产，跳过", tag)
        return None
    return {
        "version": tag.lstrip("vV"),
        "name": str(release.get("name") or tag),
        "body": str(release.get("body") or ""),
        "zip_url": zip_url,
        "html_url": str(release.get("html_url") or ""),
    }


def _cached_zip(version: str) -> Path | None:
    """缓存里已下载的同版本 zip（用户上次选了「以后再说」时可复用）。"""
    path = CACHE_DIR / f"ClipVault-v{version}-win64.zip"
    return path if path.is_file() else None


def download_update(zip_url: str, version: str) -> Path | None:
    """下载更新 zip 到 %TEMP%/ClipVault-update/，返回本地路径；失败返回 None。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target = CACHE_DIR / f"ClipVault-v{version}-win64.zip"
        request = urllib.request.Request(
            zip_url,
            headers={"User-Agent": f"ClipVault/{current_version()}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        if not data:
            return None
        # 先写临时文件再替换：下载中途失败不会留下半截 zip
        tmp = target.with_suffix(".zip.part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return target
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("更新包下载失败（%s）：%s", version, exc)
        return None


#: 安装脚本模板（.cmd，全英文避免编码问题）：
#: 等本进程退出 -> 解压覆盖安装目录 -> 重启 -> 自清理
_INSTALL_CMD = """@echo off
setlocal enabledelayedexpansion
set "PID=%~1"
set "ZIP=%~2"
set "DIR=%~3"
:wait
tasklist /FI "PID eq %PID%" 2>nul | find /i "%PID%" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%DIR%' -Force"
start "" "%DIR%\\ClipVault.exe"
del "%ZIP%" >nul 2>&1
del "%~f0" >nul 2>&1
"""


def install_dir() -> Path:
    """安装目录（exe 所在目录，即 onedir 包的 ClipVault 文件夹）。"""
    return Path(sys.executable).resolve().parent


def install_update(zip_path: Path) -> bool:
    """进入热替换流程：生成安装脚本并启动它，返回 False 表示环境不支持。

    脚本等本进程退出后才动文件，所以这里可以直接让调用方退出应用。
    非 Windows / 非打包模式返回 False（调用方提示手动更新）。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    script = CACHE_DIR / "clipvault-update.cmd"
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        script.write_text(_INSTALL_CMD, encoding="ascii")
        # DETACHED_PROCESS 让脚本脱离本进程独立存活；start 由 cmd 自己完成
        subprocess.Popen(
            [
                "cmd",
                "/c",
                "start",
                "",
                str(script),
                str(os.getpid()),
                str(zip_path),
                str(install_dir()),
            ],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    except OSError as exc:
        logger.warning("启动更新脚本失败：%s", exc)
        return False
    return True


def start_background_check(on_result) -> threading.Thread:
    """后台线程执行检查；on_result(info|None) 在子线程里被调用（调用方自行回主线程）。

    on_result 抛异常只记日志——检查更新绝不能影响应用。
    """
    def _run():
        try:
            info = check_for_update()
        except Exception as exc:  # 兜底
            logger.warning("更新检查异常：%s", exc)
            info = None
        try:
            on_result(info)
        except Exception as exc:
            logger.warning("更新检查回调异常：%s", exc)

    thread = threading.Thread(target=_run, name="clipvault-update-check", daemon=True)
    thread.start()
    return thread
