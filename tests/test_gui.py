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


def test_gui_settings_window_saves_to_settings_json(window):
    """AI 设置：打开设置窗 -> 保存配置 -> settings.json 落盘、模式按钮出现/消失。"""
    import config
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()
    assert gui.mode_buttons == {}  # 未配置 AI：没有模式按钮

    # 打开设置窗口（模态 Toplevel）
    gui.open_settings()
    window.update()
    assert gui._settings_win is not None
    assert gui._settings_win.title() == "AI 设置"

    # 两个模型下拉框已建立（分类/向量）
    assert set(gui._settings_model_boxes.keys()) == {
        "CLIPVAULT_AI_CHAT_MODEL",
        "CLIPVAULT_AI_EMBED_MODEL",
    }

    # 模拟界面填写后点「保存」：写入 settings.json 并触发统一刷新
    values = {
        "CLIPVAULT_AI_ENABLED": "1",
        "CLIPVAULT_AI_API_KEY": "sk-gui-test",
        "CLIPVAULT_AI_BASE_URL": "https://example.com/v1",
        "CLIPVAULT_AI_CHAT_MODEL": "gpt-4o-mini",
        "CLIPVAULT_AI_EMBED_MODEL": "text-embedding-3-small",
        "CLIPVAULT_AI_CATEGORIES": "链接,代码",
        "CLIPVAULT_AI_TIMEOUT": "10",
    }
    config.save_settings(values)
    gui._after_settings_changed()
    window.update()

    # 配置生效：模式按钮出现、设置按钮变为高亮色
    assert gui.ai_configured is True
    assert set(gui.mode_buttons.keys()) == {"auto", "keyword", "semantic"}
    assert config.get_setting("CLIPVAULT_AI_API_KEY") == "sk-gui-test"
    assert config.get_setting("CLIPVAULT_AI_CHAT_MODEL") == "gpt-4o-mini"

    # 「恢复默认」：清除 settings.json 后模式按钮消失
    config.clear_settings()
    gui._after_settings_changed()
    window.update()
    assert gui.mode_buttons == {}

    gui._close_settings()
    window.update()
    assert gui._settings_win is None


def test_gui_editor_window_opens_and_closes(window):
    """编辑窗：文本条目可开编辑窗（含内容文本框），图片条目仅命名。"""
    from gui import ClipVaultGUI

    image_id, text_id, _ = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    # 文本条目：编辑窗带内容框，且预填当前内容
    gui.open_editor(text_id)
    window.update()
    assert gui._editor_win is not None
    assert gui._editor_win.title() == "编辑条目"
    gui._close_editor()
    window.update()
    assert gui._editor_win is None

    # 图片条目：同样能打开（仅命名）
    gui.open_editor(image_id)
    window.update()
    assert gui._editor_win is not None
    gui._close_editor()


def test_gui_title_rendering_and_height(window):
    """命名后的卡片：高度增加一行，指纹包含名称（改名会触发重绘）。"""
    import storage
    from gui import ClipVaultGUI

    _, text_id, _ = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    item = storage.get_item(text_id)
    plain_height = gui._card_height(item, 600)
    storage.update_item_title(text_id, "我的命名")
    named = storage.get_item(text_id)
    assert gui._card_height(named, 600) == plain_height + 22

    # 指纹差异：名称变化应让 _load_and_render 重绘
    gui._fingerprint = "stale"
    gui._load_and_render()
    window.update()
    assert gui._fingerprint != "stale"
    renamed = next(r for r in gui.items if r["id"] == text_id)
    assert renamed.get("title") == "我的命名"


def test_gui_editor_save_updates_content_and_title(window):
    """编辑保存：改内容+命名落库，且内容变化会重新入队 AI 分析。"""
    import tkinter as tk

    import ai_client
    import storage
    from gui import ClipVaultGUI

    _, text_id, _ = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    enqueued: list[tuple] = []
    original_is_configured = ai_client.is_configured
    original_enqueue = ai_client.enqueue_analysis
    ai_client.is_configured = lambda: True
    ai_client.enqueue_analysis = lambda i, t: enqueued.append((i, t))
    try:
        # 伪造 content_box（.get 返回新内容）+ 命名
        class _Box:
            def get(self, *_args):
                return "编辑后的新内容\n"

        gui._save_editor(
            window,
            storage.get_item(text_id),
            True,
            _Box(),
            tk.StringVar(value="新名字"),
        )
        window.update()
    finally:
        ai_client.is_configured = original_is_configured
        ai_client.enqueue_analysis = original_enqueue

    row = storage.get_item(text_id)
    assert row["text_content"] == "编辑后的新内容"
    assert row["title"] == "新名字"
    assert enqueued == [(text_id, "编辑后的新内容")]  # H1 回归：编辑后重新入队


def test_gui_editor_save_conflict_keeps_window(window, monkeypatch):
    """编辑冲突：弹警告、不关窗、内容不变。"""
    import hashlib
    import tkinter as tk

    import gui as gui_module
    import storage
    from gui import ClipVaultGUI

    _, text_id, _ = _seed()
    # 冲突检测按真实 sha256 比对，插入时要用真实哈希
    storage.insert_item(
        "text",
        content_hash=hashlib.sha256("别人的内容".encode()).hexdigest(),
        text_content="别人的内容",
    )
    gui = ClipVaultGUI(window)
    window.update()

    warnings: list[str] = []
    monkeypatch.setattr(
        gui_module.messagebox, "showwarning", lambda title, msg, parent=None: warnings.append(msg)
    )

    class _Box:
        def get(self, *_args):
            return "别人的内容"  # 与 other 撞车

    gui._save_editor(window, storage.get_item(text_id), True, _Box(), tk.StringVar(value=""))
    window.update()

    assert warnings and "重复" in warnings[0]
    assert storage.get_item(text_id)["text_content"] != "别人的内容"


def test_gui_action_buttons_hit_test(window):
    """卡片悬停按钮 4 个：分组 < 置顶 < 编辑 < 删除，命中区域互不重叠。"""
    from gui import ClipVaultGUI

    image_id, text_id, _ = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    # 取第一张卡的几何，悬停它触发按钮绘制
    first_id = gui.items[0]["id"]
    gui.hovered_id = first_id
    gui._render()
    window.update()
    cx, cy, cw, ch = gui._card_rects[first_id]

    # 按钮宽度 52、间距 6：分组 < 置顶 < 编辑 < 删除，从左到右
    btn_w, gap = 52, 6
    bx1 = cx + cw - btn_w * 4 - gap * 3 - 8
    bx2 = bx1 + btn_w + gap
    bx3 = bx2 + btn_w + gap
    bx4 = bx3 + btn_w + gap
    assert bx1 < bx2 < bx3 < bx4

    class _Evt:
        def __init__(self, x, y):
            self.x, self.y = x, y

    # 坐标换算成画布坐标：卡片在文档流里，直接用相对坐标+偏移量
    assert gui._hit_test(_Evt(bx1 + btn_w // 2, cy + 8 + 13)) == ("action:group", first_id)
    assert gui._hit_test(_Evt(bx2 + btn_w // 2, cy + 8 + 13)) == ("action:pin", first_id)
    hit_edit = gui._hit_test(_Evt(bx3 + btn_w // 2, cy + 8 + 13))
    assert hit_edit == ("action:edit", first_id)
    hit_del = gui._hit_test(_Evt(bx4 + btn_w // 2, cy + 8 + 13))
    assert hit_del == ("action:del", first_id)
    # 卡片中部 = 复制
    hit_card = gui._hit_test(_Evt(cx + 30, cy + ch - 12))
    assert hit_card == ("card", first_id)


# ---------------------------------------------------------------------------
# 分组：左栏筛选 / 卡片分组菜单 / 分组管理 / AI 自动分组
# ---------------------------------------------------------------------------


def test_gui_group_sidebar_and_view_filter(window):
    """分组栏切换视图：全部 / 未分组 / 具体分组，列表正确过滤。"""
    import storage
    from gui import ClipVaultGUI

    image_id, text_id, pinned_id = _seed()
    group = storage.create_group("工作")
    storage.add_item_to_group(text_id, group)
    gui = ClipVaultGUI(window)
    window.update()

    # 默认「全部」视图
    assert len(gui.items) == 3
    # 「未分组」只剩图片 + 置顶文本
    gui._select_group("ungrouped")
    window.update()
    assert {r["id"] for r in gui.items} == {image_id, pinned_id}
    # 具体分组只剩分进去的那条
    gui._select_group(group)
    window.update()
    assert [r["id"] for r in gui.items] == [text_id]
    # 回「全部」
    gui._select_group("all")
    window.update()
    assert len(gui.items) == 3
    # 卡片上带分组名（meta 徽章与指纹用）
    row = next(r for r in gui.items if r["id"] == text_id)
    assert row["group_names"] == ["工作"]


def test_gui_group_view_with_search(window):
    """分组视图与关键词搜索叠加：两个条件都生效。"""
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()

    gui._placeholder_active = False
    gui.search_var.set("置顶")
    gui._select_group("ungrouped")
    window.update()
    assert [r["id"] for r in gui.items] == [gui.items[0]["id"]]
    assert gui.items[0]["text_content"] == "置顶条目"


def test_gui_group_menu_toggles_membership(window):
    """分组菜单：打开 -> 勾选加入 -> 取消，落库且指纹触发重绘。"""
    import storage
    from gui import ClipVaultGUI

    _, text_id, _ = _seed()
    group = storage.create_group("工作")
    gui = ClipVaultGUI(window)
    window.update()

    class _Evt:
        x_root, y_root = 100, 100

    gui._open_group_menu(_Evt(), text_id)
    window.update()
    assert gui._group_menu_win is not None
    assert group in gui._group_menu_vars  # 菜单列出了该分组

    gui._toggle_item_group(text_id, group, True)
    window.update()
    assert storage.item_group_ids(text_id) == [group]
    # 分组栏计数即时刷新：「未分组」从 3 变 2
    assert gui.group_nav_buttons["ungrouped"]["text"] == "未分组（2）"

    gui._toggle_item_group(text_id, group, False)
    window.update()
    assert storage.item_group_ids(text_id) == []
    assert gui.group_nav_buttons["ungrouped"]["text"] == "未分组（3）"

    gui._close_group_menu()
    window.update()
    assert gui._group_menu_win is None


def test_gui_group_management(window, monkeypatch):
    """分组管理：新建（切到新组）-> 重名警告 -> 重命名 -> 删除（条目保留）。"""
    import gui as gui_module
    import storage
    from gui import ClipVaultGUI

    _, text_id, _ = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    warnings: list[tuple] = []
    answers = iter(["工作", "开发", "新名字"])  # 新建 / 撞名 / 改名
    monkeypatch.setattr(gui_module.simpledialog, "askstring", lambda *a, **k: next(answers))
    monkeypatch.setattr(gui_module.messagebox, "showwarning", lambda *a, **k: warnings.append(a))

    gui._new_group_clicked()
    window.update()
    group = next(g for g in storage.list_groups() if g["name"] == "工作")
    assert gui.group_id == group["id"]  # 新建后直接切到该分组视图

    storage.create_group("开发")  # 另一个分组先占住「开发」这个名字
    gui._rename_group(group["id"])  # 想改成「开发」-> 撞名 -> 警告
    window.update()
    assert warnings and "已存在" in str(warnings[0])
    assert storage.get_group(group["id"])["name"] == "工作"  # 没改成

    gui._rename_group(group["id"])  # 改成「新名字」
    window.update()
    assert storage.get_group(group["id"])["name"] == "新名字"

    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *a, **k: True)
    gui._delete_group(group["id"])
    window.update()
    assert storage.get_group(group["id"]) is None
    assert storage.get_item(text_id) is not None  # 条目不受影响
    assert gui.group_id is None  # 正在看被删的组 -> 退回「全部」


def test_gui_auto_group_worker_creates_groups_and_assigns(window, monkeypatch):
    """AI 自动分组（同步部分）：未分组条目 -> 模型分配 -> 建组 -> 入组。"""
    import ai_client
    import storage
    from gui import ClipVaultGUI

    _, text_id, pinned_id = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    monkeypatch.setattr(ai_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        ai_client, "assign_groups", lambda rows, existing: {i: "AI 新组" for i, _ in rows}
    )
    message = gui._run_auto_group()
    window.update()

    assert "2 条入组" in message, message
    group = next(g for g in storage.list_groups() if g["name"] == "AI 新组")
    assert group["count"] == 2
    assert set(storage.item_group_ids(text_id)) == {group["id"]}
    assert set(storage.item_group_ids(pinned_id)) == {group["id"]}


def test_gui_auto_group_requires_ai(window, monkeypatch):
    """AI 未配置时点「AI 自动分组」：提示 + 不起后台任务（安全降级）。"""
    import ai_client
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()
    monkeypatch.setattr(ai_client, "is_configured", lambda: False)
    gui.ai_configured = False
    gui.start_auto_group()
    assert getattr(gui, "_auto_group_running", False) is False


def test_gui_fingerprint_includes_group_names(window):
    """分组归属变化应让渲染指纹变化（否则徽章不刷新）。"""
    import storage
    from gui import ClipVaultGUI

    _, text_id, _ = _seed()
    gui = ClipVaultGUI(window)
    window.update()

    row = next(r for r in gui.items if r["id"] == text_id)
    before = gui._fingerprint
    group = storage.create_group("工作")
    storage.add_item_to_group(text_id, group)
    gui._load_and_render()
    window.update()
    assert gui._fingerprint != before  # 分组名进了指纹，触发重绘
    row = next(r for r in gui.items if r["id"] == text_id)
    assert row["group_names"] == ["工作"]


def test_gui_smart_search_merges_keyword_and_semantic(window, monkeypatch):
    """智能检索：关键词（内容/命名/来源）命中排前，语义增量去重补后。

    回归：命名搜索 —— 内容里没有搜索词、但自定义命名命中的条目必须出现，
    不能只在语义结果里找（没建向量的老条目会被纯语义漏掉）。
    """
    import storage
    from gui import ClipVaultGUI

    # 内容命中
    content_id = storage.insert_item(
        "text", content_hash="ss-1", text_content="DeepSeek API 调用示例"
    )
    # 仅命名命中（内容无关）
    named_id = storage.insert_item("text", content_hash="ss-2", text_content="sk-abc123")
    storage.update_item_title(named_id, "DeepSeek 账号")
    # 仅语义命中（关键词完全匹配不到）
    semantic_only = storage.insert_item("text", content_hash="ss-3", text_content="大语言模型")

    gui = ClipVaultGUI(window)
    window.update()
    gui.mode = "auto"
    gui.ai_configured = True
    # 只让第 3 条出现在语义结果里
    monkeypatch.setattr(gui, "_semantic_rows", lambda q: [storage.get_item(semantic_only)])

    rows = gui._merge_smart_results("DeepSeek")
    ids = [row["id"] for row in rows]

    # 两条关键词命中都在语义增量之前（块内具体顺序由置顶/时间/id 决定，无关紧要）
    assert ids.index(named_id) < ids.index(semantic_only)
    assert content_id in ids  # 内容命中
    assert named_id in ids  # 命名命中必须出现（回归点）
    assert len(ids) == len(set(ids))  # 不重复


def test_gui_settings_test_uses_connection_selfcheck(window, monkeypatch):
    """「测试连接」走 ai_client.test_connection：对话接口成功也算通过。

    回归：DeepSeek 无向量接口，旧实现只测 embedding 会永远误报连接失败。
    """
    import ai_client
    import gui as gui_module
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()

    # 不落 settings.json（避免污染其他用例），自检被 monkeypatch 成「对话接口通」
    monkeypatch.setattr(gui_module.config, "save_settings", lambda values: None)
    monkeypatch.setattr(ai_client, "is_configured", lambda: True)
    monkeypatch.setattr(ai_client, "test_connection", lambda: (True, "连接成功！对话接口可用"))
    shown: list[str] = []
    monkeypatch.setattr(
        gui_module.messagebox, "showinfo", lambda title, msg, parent=None: shown.append(msg)
    )

    # 测试环境没有 mainloop：把 gui 的 threading.Thread 换成同步执行器，
    # 让后台自检在测试线程内跑完，root.after 的回调再由 window.update() 处理
    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target

        def start(self):
            self._target()

    import threading as _real_threading

    class _Shim:
        Thread = _SyncThread
        Event = _real_threading.Event

    monkeypatch.setattr(gui_module, "threading", _Shim)

    # 真实打开设置窗（_show_test_result 只在设置窗开着时才弹结果框）
    gui.open_settings()
    window.update()
    assert gui._settings_win is not None

    gui._settings_test(
        gui._settings_win, gui._settings_entries, gui._settings_enabled
    )
    window.update()  # 处理 root.after(0, _show_test_result) 回调

    gui._close_settings()
    window.update()
    assert shown and "对话接口可用" in shown[0]


# ---------------------------------------------------------------------------
# 历史上限清理：手动清理 / 自动清理钩子
# ---------------------------------------------------------------------------


def test_gui_manual_cleanup_keeps_grouped_only(window, monkeypatch):
    """「清理未分组」：二次确认后删未分组，分组内容留下。"""
    import gui as gui_module
    import storage
    from gui import ClipVaultGUI

    _seed()  # 3 条全部未分组（会被清掉）
    grouped = storage.insert_item("text", content_hash="cu-1", text_content="入组的")
    group = storage.create_group("收藏")
    storage.add_item_to_group(grouped, group)

    gui = ClipVaultGUI(window)
    window.update()
    assert storage.stats()["total"] == 4

    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *a, **k: True)
    gui._cleanup_clicked()
    window.update()

    assert storage.stats()["total"] == 1  # 只剩入组那条
    assert storage.get_item(grouped) is not None
    assert "已清理" in gui.toast_var.get()


def test_gui_manual_cleanup_cancelled_changes_nothing(window, monkeypatch):
    """取消二次确认：一条不动。"""
    import gui as gui_module
    import storage
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()
    before = storage.stats()["total"]

    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *a, **k: False)
    gui._cleanup_clicked()
    window.update()

    assert storage.stats()["total"] == before


def test_gui_auto_cleanup_hook_refreshes_on_delete(window, monkeypatch):
    """自动清理钩子：到期真删时强制刷新界面 + toast；没删则静默。"""
    import cleanup
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()

    # 没到期（maybe_run 返回 None）：不动界面
    monkeypatch.setattr(cleanup, "maybe_run", lambda: None)
    gui._fingerprint = "sentinel"
    gui._maybe_auto_cleanup()
    assert gui._fingerprint == "sentinel"

    # 到期且删了 2 条：指纹作废（触发重绘）+ toast 汇报
    monkeypatch.setattr(cleanup, "maybe_run", lambda: {"deleted": 2, "ran": True, "reason": ""})
    gui._fingerprint = "sentinel"
    gui._maybe_auto_cleanup()
    window.update()
    assert gui._fingerprint != "sentinel"
    assert "每周清理" in gui.toast_var.get()


# ---------------------------------------------------------------------------
# 自动更新：检查 -> 下载 -> 确认 -> 热替换
# ---------------------------------------------------------------------------


def test_gui_update_check_full_flow(window, monkeypatch):
    """手动检查更新：有新版本 -> 后台下载 -> 确认 -> 安装并安排退出。

    全程无网络：check/download/install 都 monkeypatch；后台线程用同步
    执行器替代（测试环境没有 mainloop，跨线程 root.after 会炸）。
    """
    from pathlib import Path

    import gui as gui_module
    import updater
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()

    info = {
        "version": "9.9.9",
        "name": "v9.9.9",
        "body": "修复了一堆问题",
        "zip_url": "https://example.com/ClipVault-v9.9.9-win64.zip",
        "html_url": "https://example.com/release",
    }
    fake_zip = gui_module.config.get_data_dir() / "fake-update.zip"
    monkeypatch.setattr(updater, "check_for_update", lambda: info)
    monkeypatch.setattr(updater, "_cached_zip", lambda ver: None)
    monkeypatch.setattr(updater, "download_update", lambda url, ver: fake_zip)
    # 检查同步完成（updater 自己的线程也垫掉，测试环境无 mainloop）
    monkeypatch.setattr(updater, "start_background_check", lambda on_result: on_result(info))
    installed: list[Path] = []
    monkeypatch.setattr(updater, "install_update", lambda p: installed.append(p) or True)
    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *a, **k: True)

    # 同步线程垫片：下载线程（gui.py 内的 threading）就地跑完
    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target

        def start(self):
            self._target()

    import threading as _real_threading

    class _Shim:
        Thread = _SyncThread
        Event = _real_threading.Event

    monkeypatch.setattr(gui_module, "threading", _Shim)

    # 防真销毁共享测试窗口：把「退出」调度（600ms）记下来而不执行
    real_after = window.after
    exits: list = []

    def _fake_after(ms, fn=None, *args):
        if ms == 600:
            exits.append(fn)
            return "fake-id"
        return real_after(ms, fn, *args)

    monkeypatch.setattr(window, "after", _fake_after)

    gui.start_update_check(manual=True)
    window.update()  # 处理 root.after(0, _on_update_checked)
    window.update()  # 处理 root.after(0, _on_update_downloaded)

    assert installed == [fake_zip]  # 确认后进入安装
    assert exits  # 安排了窗口退出（安装脚本随后接管）


def test_gui_update_check_no_new_version_manual(window, monkeypatch):
    """手动检查但没有新版本：toast 明示已是最新（自动检查则不打扰）。"""
    import updater
    from gui import ClipVaultGUI

    _seed()
    gui = ClipVaultGUI(window)
    window.update()
    monkeypatch.setattr(updater, "check_for_update", lambda: None)
    # 检查同步完成：on_result(None) 就地调用（测试环境无 mainloop，跨线程 after 会炸）
    monkeypatch.setattr(updater, "start_background_check", lambda on_result: on_result(None))

    gui.start_update_check(manual=True)
    window.update()
    assert "已是最新版本" in gui.toast_var.get()
