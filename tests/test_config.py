"""tests/test_config.py — GUI 设置文件（settings.json）与统一读取优先级。

优先级约定：settings.json（界面设置）> 环境变量/.env > 内置默认值。
"""

from __future__ import annotations

import json

import pytest

import config


@pytest.fixture(autouse=True)
def clean_settings():
    """每个用例前后清空设置文件与缓存，保证隔离。"""
    config.clear_settings()
    yield
    config.clear_settings()


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


def test_clear_settings_removes_file():
    config.save_settings({"CLIPVAULT_AI_API_KEY": "k"})
    config.clear_settings()
    assert not config.settings_path().exists()
    assert config.load_settings() == {}
