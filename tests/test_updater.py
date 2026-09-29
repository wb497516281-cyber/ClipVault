"""tests/test_updater.py — 自动更新测试：版本比较 / release 解析 / 下载 / 安装脚本。

所有网络调用都用 monkeypatch 替换，不会发起真实请求。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import config
import updater


@pytest.fixture
def frozen(monkeypatch):
    """模拟打包版运行环境（自动更新只对 frozen 生效）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)


@pytest.fixture(autouse=True)
def freeze_version(monkeypatch):
    """固定当前版本，避免版本推进后比较逻辑失效。"""
    monkeypatch.setattr(config, "APP_VERSION", "1.2.0")


def _release(tag="v1.3.0", with_zip=True):
    """构造一个 GitHub release API 响应。"""
    assets = []
    if with_zip:
        assets.append(
            {
                "name": "ClipVault-v1.3.0-win64.zip",
                "browser_download_url": "https://example.com/ClipVault-v1.3.0-win64.zip",
            }
        )
    return {
        "tag_name": tag,
        "name": f"ClipVault {tag}",
        "body": "更新说明",
        "html_url": "https://github.com/x/y/releases/tag/" + tag,
        "draft": False,
        "prerelease": False,
        "assets": assets,
    }


# ---------------------------------------------------------------------------
# 版本解析与比较
# ---------------------------------------------------------------------------


def test_parse_version_variants():
    assert updater.parse_version("v1.2.0") == (1, 2, 0)
    assert updater.parse_version("1.2.0-beta") == (1, 2, 0)  # 后缀截掉
    assert updater.parse_version("v2.0") == (2, 0)
    assert updater.parse_version("abc") is None
    assert updater.parse_version("") is None


def test_is_newer_semantics():
    assert updater.is_newer("v1.3.0", "1.2.0") is True
    assert updater.is_newer("v1.2.1", "1.2.0") is True
    assert updater.is_newer("v1.2.0", "1.2.0") is False  # 相同不算新
    assert updater.is_newer("v1.2.0", "1.2.0.0") is False  # 补齐后相等
    assert updater.is_newer("v1.2", "1.2.0") is False  # 1.2 视为 1.2.0
    assert updater.is_newer("v1.1.9", "1.2.0") is False  # 旧版不算
    assert updater.is_newer("乱码", "1.2.0") is False  # 解析失败保守不升


def test_check_skipped_in_source_mode():
    """源码模式（未 frozen）一律不检查：开发用 git，不被 release 覆盖。"""
    assert updater.check_for_update() is None


def test_check_for_update_newer(frozen, monkeypatch):
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: _release())
    info = updater.check_for_update()
    assert info is not None
    assert info["version"] == "1.3.0"
    assert info["zip_url"].endswith(".zip")


def test_check_for_update_same_or_older(frozen, monkeypatch):
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: _release(tag="v1.2.0"))
    assert updater.check_for_update() is None
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: _release(tag="v1.1.0"))
    assert updater.check_for_update() is None


def test_check_ignores_draft_and_prerelease(frozen, monkeypatch):
    for key in ("draft", "prerelease"):
        data = _release()
        data[key] = True
        monkeypatch.setattr(updater, "_get_json", lambda url, data=data, timeout=10.0: data)
        assert updater.check_for_update() is None


def test_check_without_zip_asset_returns_none(frozen, monkeypatch):
    """release 没挂 zip（比如只发了说明）：跳过，不提供下载。"""
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: _release(with_zip=False))
    assert updater.check_for_update() is None


def test_check_network_failure_returns_none(frozen, monkeypatch):
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: None)
    assert updater.check_for_update() is None


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


class _FakeResponse:
    """模拟 urlopen 响应（支持上下文管理器）。"""

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_download_update_writes_file(frozen, monkeypatch):
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda req, timeout=120: _FakeResponse(b"PK-zip")
    )
    path = updater.download_update("https://example.com/x.zip", "1.3.0")
    assert path is not None and path.is_file()
    assert path.read_bytes() == b"PK-zip"
    assert path.name == "ClipVault-v1.3.0-win64.zip"
    path.unlink()


def test_download_failure_returns_none(frozen, monkeypatch):
    def boom(req, timeout=120):
        raise OSError("网络断了")

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    assert updater.download_update("https://example.com/x.zip", "1.3.0") is None


# ---------------------------------------------------------------------------
# 安装（热替换脚本）
# ---------------------------------------------------------------------------


def test_install_update_writes_script_and_starts(frozen, monkeypatch, tmp_path):
    """安装：生成 .cmd（含进程等待/解压/重启）并启动。"""
    zip_path = tmp_path / "ClipVault-v1.3.0-win64.zip"
    zip_path.write_bytes(b"PK")
    started: list[list[str]] = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            started.append(list(args))

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    ok = updater.install_update(zip_path)
    if ok:  # Windows 环境：验证脚本内容
        assert started and "cmd" in started[0]
        script = updater.CACHE_DIR / "clipvault-update.cmd"
        assert script.is_file()
        content = script.read_text(encoding="ascii")
        assert "Expand-Archive" in content and "ClipVault.exe" in content
        assert str(zip_path) in started[0]  # zip 路径与安装目录都传给了脚本
        script.unlink()


def test_install_update_rejected_without_frozen():
    """非打包模式（源码运行）不允许热替换，返回 False。"""
    assert updater.install_update(Path("nonexistent.zip")) is False


def test_start_background_check_survives_callback_error(frozen, monkeypatch):
    """检查/回调异常都被吞掉，不影响调用方。"""
    monkeypatch.setattr(updater, "check_for_update", lambda: (_ for _ in ()).throw(OSError("boom")))
    calls: list = []
    thread = updater.start_background_check(lambda info: calls.append(info))
    thread.join(timeout=5)
    assert calls == [None]
