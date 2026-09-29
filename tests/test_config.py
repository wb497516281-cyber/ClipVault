"""tests/test_config.py — GUI 设置文件（settings.json）、统一读取优先级与数据目录策略。

优先级约定：settings.json（界面设置）> 环境变量/.env > 内置默认值。
数据目录：CLIPVAULT_DATA_DIR > 打包版 %LOCALAPPDATA%/ClipVault/data > 源码版 clipboard_data。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import config


@pytest.fixture(autouse=True)
def clean_settings():
    """每个用例前后清空设置文件与缓存，保证隔离。"""
    config.clear_settings()
    yield
    config.clear_settings()


# ---------------------------------------------------------------------------
# .env 解析
# ---------------------------------------------------------------------------


def test_load_env_file_basic(tmp_path, monkeypatch):
    """基本解析：KEY=VALUE 入环境，不覆盖已有变量。"""
    env = tmp_path / ".env"
    env.write_text("FOO_KEY=bar\nIGNORED\n", encoding="utf-8")
    monkeypatch.delenv("FOO_KEY", raising=False)
    monkeypatch.setenv("EXISTING_KEY", "keep")
    env.write_text("FOO_KEY=bar\nEXISTING_KEY=override\n", encoding="utf-8")
    config.load_env_file(env)
    import os

    assert os.environ["FOO_KEY"] == "bar"
    assert os.environ["EXISTING_KEY"] == "keep"  # 不覆盖已有


def test_load_env_file_quotes_export_and_comment(tmp_path, monkeypatch):
    """成对引号 / export 前缀 / 行内注释 / 空行注释行。"""
    env = tmp_path / ".env"
    env.write_text(
        "# 注释行\n"
        "\n"
        'export QUOTED="hello world"\n'
        "SINGLE='single'\n"
        "WITH_COMMENT=value # 这是备注\n"
        "NO_EQUALS_LINE\n",
        encoding="utf-8",
    )
    for key in ("QUOTED", "SINGLE", "WITH_COMMENT"):
        monkeypatch.delenv(key, raising=False)
    config.load_env_file(env)
    import os

    assert os.environ["QUOTED"] == "hello world"
    assert os.environ["SINGLE"] == "single"
    assert os.environ["WITH_COMMENT"] == "value"


def test_load_env_file_bom(tmp_path, monkeypatch):
    """记事本另存的带 BOM .env：首个键名不能带 \\ufeff。"""
    env = tmp_path / ".env"
    env.write_bytes("BOM_KEY=first\n".encode("utf-8-sig"))
    monkeypatch.delenv("BOM_KEY", raising=False)
    config.load_env_file(env)
    import os

    assert os.environ["BOM_KEY"] == "first"


def test_load_env_file_non_utf8_does_not_crash(tmp_path, monkeypatch):
    """GBK 等非 UTF-8 .env 不能让进程崩（errors=replace 容错）。"""
    env = tmp_path / ".env"
    env.write_bytes("GBK_KEY=中文".encode("gbk"))
    monkeypatch.delenv("GBK_KEY", raising=False)
    config.load_env_file(env)  # 不抛异常即通过
    import os

    assert "GBK_KEY" in os.environ


def test_save_and_load_roundtrip():
    values = {
        "CLIPVAULT_AI_API_KEY": "sk-test-123",
        "CLIPVAULT_AI_BASE_URL": "https://example.com/v1",
        "CLIPVAULT_AI_ENABLED": "0",
    }
    config.save_settings(values)
    assert config.settings_path().is_file()
    assert config.load_settings() == values


def test_settings_file_lives_in_data_dir():
    """设置文件必须在数据目录内（随数据目录搬家、被 gitignore）。"""
    assert config.settings_path().parent == config.get_data_dir()


def test_get_setting_priority_settings_over_env(monkeypatch):
    """界面设置优先于环境变量。"""
    monkeypatch.setenv("CLIPVAULT_AI_API_KEY", "env-key")
    assert config.get_setting("CLIPVAULT_AI_API_KEY", "") == "env-key"

    config.save_settings({"CLIPVAULT_AI_API_KEY": "gui-key"})
    assert config.get_setting("CLIPVAULT_AI_API_KEY", "") == "gui-key"


def test_get_setting_falls_back_to_env_then_default(monkeypatch):
    """无界面设置时用环境变量；都没有则用默认值。"""
    monkeypatch.setenv("CLIPVAULT_AI_BASE_URL", "https://env.example/v1")
    assert config.get_setting("CLIPVAULT_AI_BASE_URL", "https://default/v1") == (
        "https://env.example/v1"
    )
    monkeypatch.delenv("CLIPVAULT_AI_BASE_URL", raising=False)
    assert config.get_setting("CLIPVAULT_AI_BASE_URL", "https://default/v1") == (
        "https://default/v1"
    )


def test_get_setting_ignores_blank_values(monkeypatch):
    """空字符串设置项视为未设置（继续往下找）。"""
    monkeypatch.setenv("CLIPVAULT_AI_API_KEY", "env-key")
    config.save_settings({"CLIPVAULT_AI_API_KEY": "   "})
    assert config.get_setting("CLIPVAULT_AI_API_KEY", "") == "env-key"


def test_corrupt_settings_file_tolerated():
    """settings.json 损坏时返回空字典，不抛异常（降级到 env/默认值）。"""
    config.get_data_dir().mkdir(parents=True, exist_ok=True)
    config.settings_path().write_text("{ 这不是合法 JSON", encoding="utf-8")
    config.clear_settings()  # 清缓存，强制重新读文件
    assert config.load_settings() == {}


def test_external_edit_invalidates_cache():
    """缓存按 mtime 失效：外部（另一个进程）改了文件也能读到新值。"""
    config.save_settings({"CLIPVAULT_AI_CHAT_MODEL": "model-a"})
    assert config.get_setting("CLIPVAULT_AI_CHAT_MODEL", "") == "model-a"

    # 绕过 save_settings 直接改文件（模拟外部编辑），并 guarantee mtime 变化
    import os
    import time

    path = config.settings_path()
    data = {"CLIPVAULT_AI_CHAT_MODEL": "model-b"}
    path.write_text(json.dumps(data), encoding="utf-8")
    future = time.time() + 10
    os.utime(path, (future, future))

    assert config.get_setting("CLIPVAULT_AI_CHAT_MODEL", "") == "model-b"


def test_save_settings_is_atomic_no_tmp_left():
    """保存走「临时文件 + os.replace」：不留 .tmp 残留，内容完整。"""
    config.save_settings({"CLIPVAULT_AI_API_KEY": "k"})
    leftovers = list(config.settings_path().parent.glob("*.tmp"))
    assert leftovers == []
    assert config.settings_path().exists()
    assert config.load_settings()["CLIPVAULT_AI_API_KEY"] == "k"


def test_clear_settings_removes_file():
    config.save_settings({"CLIPVAULT_AI_API_KEY": "k"})
    config.clear_settings()
    assert not config.settings_path().exists()
    assert config.load_settings() == {}


# ---------------------------------------------------------------------------
# 数据目录策略：源码 / 打包 / 环境变量 / 一次性迁移
# ---------------------------------------------------------------------------


def test_app_version_matches_pyproject():
    """config.APP_VERSION 与 pyproject.toml 的 version 必须一致（防发版漂移）。"""
    pyproject = Path(config.__file__).resolve().parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    marker = 'version = "'
    line = next(ln for ln in text.splitlines() if ln.startswith(marker))
    pyproject_version = line[len(marker) :].split('"', 1)[0]
    assert config.APP_VERSION == pyproject_version


def test_data_dir_env_override_wins(monkeypatch, tmp_path):
    """CLIPVAULT_DATA_DIR 优先于一切默认值。"""
    monkeypatch.setenv("CLIPVAULT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert config.get_data_dir() == tmp_path.resolve()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.get_data_dir() == tmp_path.resolve()  # frozen 也一样优先


def test_data_dir_frozen_uses_localappdata(monkeypatch, tmp_path):
    """打包版默认 %LOCALAPPDATA%/ClipVault/data —— exe 外面，重建不丢数据。"""
    monkeypatch.delenv("CLIPVAULT_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.get_data_dir() == tmp_path / "ClipVault" / "data"


def test_data_dir_source_mode_uses_project_dir(monkeypatch):
    """源码模式默认 <项目根>/clipboard_data（开发便利）。"""
    monkeypatch.delenv("CLIPVAULT_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert config.get_data_dir() == config.BASE_DIR / "clipboard_data"


def test_migrate_moves_legacy_dir_in_frozen_mode(monkeypatch, tmp_path):
    """打包版首启：exe 旁旧 clipboard_data 整体搬到 %LOCALAPPDATA%/ClipVault/data。"""
    # 旧的安装目录（exe 旁带数据）
    install_dir = tmp_path / "ClipVault"
    legacy = install_dir / "clipboard_data"
    (legacy / "images").mkdir(parents=True)
    (legacy / "clipboard.db").write_bytes(b"db-bytes")
    (legacy / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "BASE_DIR", install_dir)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("CLIPVAULT_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    config.migrate_legacy_data_dir()

    target = tmp_path / "AppData" / "Local" / "ClipVault" / "data"
    assert (target / "clipboard.db").read_bytes() == b"db-bytes"
    assert (target / "settings.json").is_file()
    assert not legacy.exists()  # 旧位置搬空


def test_migrate_skipped_when_target_exists(monkeypatch, tmp_path):
    """新目录已有数据时绝不覆盖（用户可能在两处都留了数据）。"""
    install_dir = tmp_path / "ClipVault"
    legacy = install_dir / "clipboard_data"
    legacy.mkdir(parents=True)
    (legacy / "clipboard.db").write_bytes(b"old")
    monkeypatch.setattr(config, "BASE_DIR", install_dir)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("CLIPVAULT_DATA_DIR", raising=False)
    local = tmp_path / "AppData" / "Local"
    target = local / "ClipVault" / "data"
    target.mkdir(parents=True)
    (target / "clipboard.db").write_bytes(b"new")
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    config.migrate_legacy_data_dir()

    assert (target / "clipboard.db").read_bytes() == b"new"  # 原样不动
    assert legacy.exists()  # 旧数据保留原地，等用户自己处理


def test_migrate_skipped_in_source_mode(monkeypatch, tmp_path):
    """源码模式不迁移（项目内 clipboard_data 就是开发数据，不是历史包袱）。"""
    project = tmp_path / "project"
    legacy = project / "clipboard_data"
    legacy.mkdir(parents=True)
    (legacy / "clipboard.db").write_bytes(b"dev")
    monkeypatch.setattr(config, "BASE_DIR", project)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("CLIPVAULT_DATA_DIR", raising=False)

    config.migrate_legacy_data_dir()

    assert (legacy / "clipboard.db").read_bytes() == b"dev"


def test_migrate_skipped_when_env_override(monkeypatch, tmp_path):
    """显式 CLIPVAULT_DATA_DIR 时用户自管位置，不搬。"""
    install_dir = tmp_path / "ClipVault"
    legacy = install_dir / "clipboard_data"
    legacy.mkdir(parents=True)
    (legacy / "clipboard.db").write_bytes(b"old")
    custom = tmp_path / "custom"
    monkeypatch.setattr(config, "BASE_DIR", install_dir)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("CLIPVAULT_DATA_DIR", str(custom))

    config.migrate_legacy_data_dir()

    assert (legacy / "clipboard.db").read_bytes() == b"old"  # 没动
    assert not custom.exists()
