"""tests/test_gui.py — 原生 GUI 冒烟测试。

真实构造 tkinter 窗口、灌入数据、触发渲染与交互，验证不抛异常。
需要显示环境（桌面会话）；无 tkinter 的环境自动跳过。
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter", reason="当前环境没有 tkinter，跳过 GUI 冒烟")


@pytest.fixture(scope="session")
def tk_root():
    """整个测试会话共用一个 Tk 根窗口。

    同一进程反复创建 Tk() 会触发 ttk 主题加载失败（tk 的已知怪癖），
    所以根窗口只建一次，用例之间靠清理控件隔离。
    """
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()  # 测试期间不显示，避免抢焦点
    yield root
    root.destroy()


@pytest.fixture()
def window(tk_root):
    """每个用例一个干净的窗口：停掉上个用例的刷新链并清空控件。"""
    old = getattr(tk_root, "_clipvault_gui", None)
    if old is not None:
        old.stop_refresh()
    for widget in tk_root.winfo_children():
        widget.destroy()
    yield tk_root
    current = getattr(tk_root, "_clipvault_gui", None)
    if current is not None:
        current.stop_refresh()


def _seed() -> tuple[int, int, int]:
    """灌 2 文本 + 1 图片（真实落盘），返回 (image_id, text_id, pinned_id)。"""
    from PIL import Image

    import storage

    storage.init_db()
    img = Image.new("RGB", (300, 200), (60, 120, 200))
    img.save(storage.IMAGE_DIR / "gui_probe.png")
    thumb = img.resize((200, 200))
    thumb.save(storage.IMAGE_DIR / "thumb_gui_probe.png")

    image_id = storage.insert_item(
        "image",
        content_hash="gui-img-1",
        image_path=storage.to_relative(storage.IMAGE_DIR / "gui_probe.png"),
        thumbnail_path=storage.to_relative(storage.IMAGE_DIR / "thumb_gui_probe.png"),
        source_app="Explorer",
    )
    text_id = storage.insert_item(
        "text",
        content_hash="gui-text-1",
        text_content="GUI 冒烟测试文本" * 20,
        source_app="Notepad",
    )
    pinned_id = storage.insert_item(
        "text", content_hash="gui-text-2", text_content="置顶条目", source_app="Chrome"
    )
    storage.update_pin(pinned_id, True)
    return image_id, text_id, pinned_id


def test_gui_renders_all_cards(window):
    """构造 GUI：标题、计数、卡片几何都应正确生成。"""
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()

    assert window.title() == "ClipVault · 剪贴板历史"
    assert gui.count_var.get() == "3 条"
    # 每张卡都记录了几何信息（画布命中检测依赖它）
    assert len(gui._card_rects) == 3
    # 置顶条目排最前
    assert gui.items[0]["is_pinned"] == 1


def test_gui_type_filter_and_search(window):
    """类型筛选与搜索都只改变展示集合，不抛异常。"""
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()

    gui._on_type_filter("image")
    window.update()
    assert len(gui.items) == 1
    assert gui.items[0]["content_type"] == "image"

    gui._on_type_filter("all")
    # 模拟输入搜索词（绕过占位符）
    gui._placeholder_active = False
    gui.search_var.set("置顶")
    gui._apply_search()
    window.update()
    assert len(gui.items) == 1
    assert gui.items[0]["text_content"] == "置顶条目"


def test_gui_pin_and_delete_two_step(window):
    """置顶切换 + 删除两步确认（第二次才真删，图片条目连带删文件）。"""
    import storage
    from gui import ClipVaultGUI

    image_id, text_id, pinned_id = _seed()
    gui = ClipVaultGUI(window)
    window.update()
    assert storage.stats()["total"] == 3

    # 取消置顶：置顶状态入库
    gui._toggle_pin(pinned_id)
    assert storage.get_item(pinned_id)["is_pinned"] == 0

    # 删除文本条目：两步确认，第一次只进入确认态
    gui._handle_delete_click(text_id)
    assert gui.pending_delete_id == text_id
    assert storage.get_item(text_id) is not None  # 还没删

    # 第二次才真删（行 + 向量）
    gui._handle_delete_click(text_id)
    window.update()
    assert storage.get_item(text_id) is None
    assert gui.pending_delete_id is None

    # 删图片条目：原图与缩略图文件都应被清理
    gui._handle_delete_click(image_id)
    gui._handle_delete_click(image_id)
    window.update()
    assert storage.get_item(image_id) is None
    assert not (storage.IMAGE_DIR / "gui_probe.png").exists()
    assert not (storage.IMAGE_DIR / "thumb_gui_probe.png").exists()


def test_gui_copy_actions_do_not_crash(window):
    """复制动作（文本/图片）在剪贴板可用/不可用时都不抛异常。"""
    from gui import ClipVaultGUI

    image_id, text_id, _ = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    gui._copy_item(text_id)  # 文本 -> CF_UNICODETEXT
    gui._copy_item(image_id)  # 图片 -> 多格式
    window.update()

    # 剪贴板可用时校验格式确实写进去了
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            png_format = win32clipboard.RegisterClipboardFormat("PNG")
            assert win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB)
            assert win32clipboard.IsClipboardFormatAvailable(png_format)
        finally:
            win32clipboard.CloseClipboard()
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.CloseClipboard()
    except Exception:
        pass  # 剪贴板不可用的环境跳过校验
