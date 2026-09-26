"""tests/conftest.py — pytest 公共夹具：隔离数据目录 + 每用例清库。

关键点：storage 在 import 时根据 CLIPVAULT_DATA_DIR 决定数据库位置，
所以必须在任何业务模块导入「之前」把该环境变量指向临时目录
（pytest 保证 conftest 先于测试模块导入）。
"""

from __future__ import annotations

import atexit
import os
import queue
import shutil
import sys
import tempfile
from contextlib import closing
from pathlib import Path

# 项目根目录加入 sys.path（pytest 从仓库根运行时可省略，双保险）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 测试专用数据目录（整个测试会话共享，用例之间靠清库隔离）
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="clipvault-tests-"))
os.environ["CLIPVAULT_DATA_DIR"] = str(_TEST_DATA_DIR)
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)  # 会话结束清理，防 /tmp 泄漏

# 默认关闭 AI：即使测试机配了 Key，测试也不会发起真实网络请求
# （test_ai_client.py 里用 monkeypatch 按需打开）
os.environ.setdefault("CLIPVAULT_AI_ENABLED", "0")

import pytest  # noqa: E402

import ai_client  # noqa: E402
import storage  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """每个用例前后清空业务表，保证用例相互独立。"""
    storage.init_db()
    with closing(storage.get_connection()) as conn:
        conn.execute("DELETE FROM clipboard_items")
        conn.execute("DELETE FROM clip_vectors")
        conn.commit()
    yield
    with closing(storage.get_connection()) as conn:
        conn.execute("DELETE FROM clipboard_items")
        conn.execute("DELETE FROM clip_vectors")
        conn.commit()


@pytest.fixture(autouse=True)
def drain_ai_queue():
    """每个用例后排空 AI 任务队列。

    worker 线程是会话级常驻的：若有用例入队但没 join 完，
    monkeypatch 拆除后残留任务可能写库污染后续用例。
    """
    yield
    while True:
        try:
            ai_client._job_queue.get_nowait()
            ai_client._job_queue.task_done()
        except queue.Empty:
            break
