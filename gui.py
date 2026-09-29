"""gui.py — ClipVault 原生桌面界面（tkinter + Canvas 画布）。

无 Web、无浏览器：列表卡片直接画在 tkinter Canvas 上。
功能：
  1. 顶部搜索框（300ms 防抖，搜内容/自定义命名/来源应用）+ 类型筛选（全部/文本/图片）；
  2. 配置 AI 后出现检索模式切换（智能=关键词+语义混合 / 关键词 / 语义）；
  3. 左侧分组栏：全部 / 未分组 / 我的分组（带计数），支持新建/重命名/删除；
  4. 卡片：文本显示前若干字符，图片显示缩略图；元信息含时间/来源/类型/AI 分类/分组/名称；
  5. 点击卡片 → 写回系统剪贴板（文本 CF_UNICODETEXT；图片多格式，QQ/微信可粘贴）；
  6. 悬停卡片出现「分组 / 置顶 / 编辑 / 删除」按钮；删除两步确认；
  7. 「分组」按钮弹出成员菜单：勾选加入/移出分组、现场新建分组、✨AI 建议本条去哪组；
  8. 分组栏「AI 自动分组」：后台把未分组条目分批交给分类模型归组（未配置 AI 自动降级）；
  9. 🧹 历史上限：未分组内容每周自动清理一次（分组内容永久保留），分组栏「清理未分组」可手动触发；
  10. 🔆 自动更新（打包版）：启动后台检查 GitHub Release，有新版本就下载，确认后重启热替换；
  11. 置顶条目排最前且不同底色 + 左侧强调条；
  12. 每 5 秒自动刷新（数据没变不重绘，不闪）；剪贴板采集在后台线程运行；
  13. 语义检索走后台线程 + 查询向量缓存，接口慢不冻结界面。

运行：python gui.py            （默认同时启动采集器）
      python gui.py --no-watch （只看界面，不采集）

依赖：tkinter（Python 内置）、Pillow；采集需要 pywin32。
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
except ImportError:  # 非 Windows / 精简 Python 环境
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    simpledialog = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

import ai_client
import cleanup
import clipwriter
import config
import storage
import updater
from config import BASE_DIR

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 每 5 秒自动刷新一次（采集器在后台持续记录）
AUTO_REFRESH_MS = 5000

#: 搜索防抖
SEARCH_DEBOUNCE_MS = 300

#: 删除二次确认的有效期
DELETE_CONFIRM_MS = 3000

#: 单次拉取条数上限
LIST_LIMIT = 200

#: 主题色（深色）
C_BG = "#15181d"  # 窗口底
C_CARD = "#1e222a"  # 卡片
C_CARD_HOVER = "#242935"  # 卡片悬停
C_CARD_PINNED = "#3a3320"  # 置顶卡片
C_PINNED_BAR = "#d9a92c"  # 置顶左侧强调条
C_BORDER = "#2e3540"
C_TEXT = "#e8ecf1"
C_DIM = "#8b96a5"
C_ACCENT = "#5b9bff"
C_DANGER = "#ef6b67"
C_SIDEBAR = "#10131a"  # 左侧分组栏底色（比窗口底再深一档）

#: 卡片几何
CARD_PAD = 10  # 卡片间距
CARD_MARGIN = 12  # 画布边距
THUMB_SIZE = 150  # 缩略图边长
TEXT_PREVIEW_CHARS = 300  # 文本预览字符数
META_HEIGHT = 24  # 元信息行高
BTN_HEIGHT = 26  # 悬停按钮高

#: 左侧分组栏宽度
SIDEBAR_WIDTH = 186

#: 卡片悬停操作按钮：分组 / 置顶 / 编辑 / 删除（从左到右）
ACTION_BTN_W = 52
ACTION_BTN_GAP = 6


def _fmt_time(created_at: str) -> str:
    """数据库时间字符串 -> 相对时间（title 里保留绝对时间由画布 tooltip 处理）。"""
    if not created_at:
        return "-"
    try:
        then = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return created_at
    diff = max(0, int((datetime.now() - then).total_seconds()))
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    if diff < 86400 * 30:
        return f"{diff // 86400} 天前"
    return f"{diff // 86400 // 30} 个月前"


class ClipVaultGUI:
    """ClipVault 主窗口：顶栏控件 + Canvas 画布列表 + 底部提示。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("ClipVault · 剪贴板历史")
        root.geometry("1060x660")
        root.minsize(720, 420)
        root.configure(bg=C_BG)

        # 运行状态
        self.items: list[dict] = []  # 当前展示的条目
        self.type_filter = "all"  # all / text / image
        self.mode = "auto"  # auto / keyword / semantic
        self.ai_configured = ai_client.is_configured()
        self.hovered_id: int | None = None  # 悬停中的卡片
        self.pending_delete_id: int | None = None
        self._pending_delete_timer: str | None = None
        self._search_timer: str | None = None
        self._img_refs: list = []  # PhotoImage 引用，防 GC
        self._fingerprint: str | None = None  # 上次渲染的数据指纹
        self._card_rects: dict[int, tuple[int, int, int, int]] = {}  # id -> (x,y,w,h)
        self._refresh_stopped = False  # 停止自动刷新链路用
        # 句柄挂到 root 上：测试复用同一根窗口 / 托盘拆卸时可停掉刷新链
        root._clipvault_gui = self  # noqa: SLF001
        # 语义检索的查询向量缓存：同一查询词不反复请求接口
        self._query_vec_cache: dict[str, list[float]] = {}
        # —— 分组状态 ——
        self.groups: dict[str, Any] = {"total": 0, "ungrouped": 0, "groups": []}  # 分组总览
        self._group_sig: str | None = None  # 分组栏指纹（不变不重建，防闪烁）
        self.group_id: int | None = None  # 当前查看的分组（None = 不在具体分组视图）
        self.show_ungrouped: bool = False  # 当前是否查看「未分组」
        self._group_menu_win: tk.Toplevel | None = None  # 卡片「分组」成员菜单

        # 升级首启兜底：登记清理时钟（没有状态文件时 7 天后才第一次真删）
        cleanup.ensure_state()

        self._build_widgets()
        self._load_and_render()
        self._schedule_refresh()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        """左侧分组栏 + 顶栏 + 画布 + 滚动条 + 提示条。"""
        self._build_sidebar()

        # —— 右侧主区（顶栏 + 画布 + 提示） ——
        main = tk.Frame(self.root, bg=C_BG)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        top = tk.Frame(main, bg=C_BG)
        top.pack(fill=tk.X, padx=10, pady=(10, 6))

        # —— 搜索框 ——
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search_changed)
        search_entry = tk.Entry(
            top,
            textvariable=self.search_var,
            bg=C_CARD,
            fg=C_TEXT,
            insertbackground=C_TEXT,
            highlightbackground=C_BORDER,
            highlightcolor=C_ACCENT,
            highlightthickness=1,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 11),
        )
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        search_entry.insert(0, "")
        self._set_placeholder(search_entry, "搜索剪贴板内容、命名或来源应用…")

        # —— 类型筛选 ——
        self.type_buttons: dict[str, tk.Button] = {}
        for label, key in (("全部", "all"), ("文本", "text"), ("图片", "image")):
            btn = tk.Button(
                top,
                text=label,
                command=lambda k=key: self._on_type_filter(k),
                bg=C_ACCENT if key == "all" else C_CARD,
                fg="#ffffff" if key == "all" else C_DIM,
                activebackground=C_CARD_HOVER,
                activeforeground=C_TEXT,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 10),
                padx=10,
                pady=2,
                cursor="hand2",
            )
            btn.pack(side=tk.LEFT, padx=(8, 0))
            self.type_buttons[key] = btn

        # —— 检索模式容器（仅配置 AI 时才有按钮；设置窗口可动态增删） ——
        self.mode_frame = tk.Frame(top, bg=C_BG)
        self.mode_frame.pack(side=tk.LEFT)
        self.mode_buttons: dict[str, tk.Button] = {}
        self._rebuild_mode_buttons()

        # —— AI 设置按钮 ——
        settings_btn = tk.Button(
            top,
            text="AI 设置",
            command=self.open_settings,
            bg=C_CARD,
            fg=C_DIM if not self.ai_configured else C_ACCENT,
            activebackground=C_CARD_HOVER,
            activeforeground=C_TEXT,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=2,
            cursor="hand2",
        )
        settings_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.settings_btn = settings_btn

        # —— 计数标签 ——
        self.count_var = tk.StringVar(value="")
        count_label = tk.Label(
            top,
            textvariable=self.count_var,
            bg=C_BG,
            fg=C_DIM,
            font=("Microsoft YaHei UI", 10),
        )
        count_label.pack(side=tk.RIGHT, padx=(8, 2))

        # —— 画布 + 滚动条 ——
        canvas_frame = tk.Frame(main, bg=C_BG)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        self.canvas = tk.Canvas(
            canvas_frame,
            bg=C_BG,
            highlightthickness=0,
            bd=0,
        )
        scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 画布事件：点击 / 悬停 / 滚轮 / 尺寸变化
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # —— 底部提示（toast） ——
        self.toast_var = tk.StringVar(value="")
        self.toast_label = tk.Label(
            main,
            textvariable=self.toast_var,
            bg="#222831",  # 注意：tkinter 只接受 6 位 hex，不能带透明度后缀
            fg="#ffffff",
            font=("Microsoft YaHei UI", 10),
            padx=14,
            pady=6,
        )
        self.toast_label.place(relx=0.5, rely=0.94, anchor=tk.CENTER)
        self.toast_label.place_forget()

        # 窗口图标（assets/tray.png 存在时）
        # 注意：图标引用单独持有 —— _img_refs 会在每次重绘时清空，
        # 混用会导致窗口图标在某些平台被 GC。
        icon_path = BASE_DIR / "assets" / "tray.png"
        if icon_path.is_file():
            try:
                from PIL import ImageTk

                self._icon_ref = ImageTk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self._icon_ref)
            except Exception:
                pass

    def _set_placeholder(self, entry: tk.Entry, text: str) -> None:
        """灰字占位符（聚焦清空、失焦恢复）。"""
        self._placeholder = text
        self._placeholder_active = True

        def on_focus_in(_event):
            if self._placeholder_active:
                entry.delete(0, tk.END)
                entry.configure(fg=C_TEXT)
                self._placeholder_active = False

        def on_focus_out(_event):
            if not entry.get():
                entry.insert(0, text)
                entry.configure(fg=C_DIM)
                self._placeholder_active = True

        entry.insert(0, text)
        entry.configure(fg=C_DIM)
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    # ------------------------------------------------------------------
    # 左侧分组栏：全部 / 未分组 / 我的分组（新建、重命名、删除、筛选）
    # ------------------------------------------------------------------

    def _build_sidebar(self) -> None:
        """搭建分组栏骨架（静态部分）；动态分组按钮由 _rebuild_sidebar 填充。"""
        self.sidebar = tk.Frame(self.root, bg=C_SIDEBAR, width=SIDEBAR_WIDTH)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)  # 固定宽度，不随内容伸缩

        # —— 新建分组按钮（沉底） ——
        new_group_btn = tk.Button(
            self.sidebar,
            text="＋ 新建分组",
            command=self._new_group_clicked,
            bg=C_CARD,
            fg=C_TEXT,
            activebackground=C_CARD_HOVER,
            activeforeground=C_TEXT,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=4,
            cursor="hand2",
        )
        new_group_btn.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=8)

        # —— 手动清理未分组（沉底，在新建分组上方） ——
        cleanup_btn = tk.Button(
            self.sidebar,
            text="🧹 清理未分组",
            command=self._cleanup_clicked,
            bg=C_CARD,
            fg=C_TEXT,
            activebackground=C_CARD_HOVER,
            activeforeground=C_TEXT,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=4,
            cursor="hand2",
        )
        cleanup_btn.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))
        self.cleanup_btn = cleanup_btn

        # —— 标题 ——
        tk.Label(
            self.sidebar,
            text="🗂 分组",
            bg=C_SIDEBAR,
            fg=C_TEXT,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side=tk.TOP, anchor=tk.W, padx=12, pady=(12, 6))

        # —— AI 自动分组（未配置 AI 时置灰，安全降级） ——
        self.auto_group_btn = tk.Button(
            self.sidebar,
            text="🤖 AI 自动分组",
            command=self.start_auto_group,
            bg=C_ACCENT if self.ai_configured else C_CARD,
            fg="#ffffff" if self.ai_configured else C_DIM,
            activebackground=C_CARD_HOVER,
            activeforeground=C_TEXT,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=3,
            cursor="hand2",
            state=tk.NORMAL if self.ai_configured else tk.DISABLED,
        )
        self.auto_group_btn.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 8))

        # —— 固定导航：全部 / 未分组（计数在 _rebuild_sidebar 更新） ——
        self.group_nav_buttons: dict[str, tk.Button] = {}
        for key, label in (("all", "全部条目"), ("ungrouped", "未分组")):
            btn = tk.Button(
                self.sidebar,
                text=label,
                command=lambda k=key: self._select_group(k),
                bg=C_ACCENT,
                fg="#ffffff",
                activebackground=C_CARD_HOVER,
                activeforeground=C_TEXT,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 10),
                padx=10,
                pady=3,
                anchor=tk.W,
                cursor="hand2",
            )
            btn.pack(side=tk.TOP, fill=tk.X, padx=8, pady=1)
            self.group_nav_buttons[key] = btn

        # —— 分隔线 + 分组列表容器 ——
        tk.Frame(self.sidebar, bg=C_BORDER, height=1).pack(
            side=tk.TOP, fill=tk.X, padx=12, pady=6
        )
        self.group_list_frame = tk.Frame(self.sidebar, bg=C_SIDEBAR)
        self.group_list_frame.pack(side=tk.TOP, fill=tk.X)
        self.group_row_buttons: dict[int, tk.Button] = {}

    def _rebuild_sidebar(self) -> None:
        """刷新分组栏数据；分组指纹没变就跳过（常驻刷新不闪烁）。"""
        try:
            overview = storage.group_overview()
        except Exception:
            return  # 数据库异常时保留上次状态，不打断主流程
        groups = overview["groups"]
        sig = (
            f"{overview['total']}|{overview['ungrouped']}|"
            + "|".join(f"{g['id']}:{g['name']}:{g['count']}" for g in groups)
        )
        if sig == self._group_sig:
            return
        self._group_sig = sig
        self.groups = overview

        # 固定导航的计数文本
        self.group_nav_buttons["all"].configure(text=f"全部条目（{overview['total']}）")
        self.group_nav_buttons["ungrouped"].configure(text=f"未分组（{overview['ungrouped']}）")

        # 动态分组按钮整体重建（条目量很小，重建比 diff 更省事）
        for btn in self.group_row_buttons.values():
            btn.destroy()
        self.group_row_buttons = {}
        for group in groups:
            name = group["name"]
            display = name if len(name) <= 12 else name[:11] + "…"
            btn = tk.Button(
                self.group_list_frame,
                text=f"{display}（{group['count']}）",
                command=lambda gid=group["id"]: self._select_group(gid),
                bg=C_CARD,
                fg=C_TEXT,
                activebackground=C_CARD_HOVER,
                activeforeground=C_TEXT,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 9),
                padx=10,
                pady=3,
                anchor=tk.W,
                cursor="hand2",
            )
            # 右键菜单：重命名 / 删除分组
            btn.bind("<Button-3>", lambda event, gid=group["id"]: self._on_group_right_click(event, gid))
            btn.pack(fill=tk.X, padx=8, pady=1)
            self.group_row_buttons[group["id"]] = btn
        self._paint_group_button_styles()

    def _paint_group_button_styles(self) -> None:
        """按当前选中状态刷新分组栏按钮配色。"""
        for key, btn in self.group_nav_buttons.items():
            active = (key == "all" and not self.show_ungrouped and self.group_id is None) or (
                key == "ungrouped" and self.show_ungrouped
            )
            btn.configure(bg=C_ACCENT if active else C_CARD, fg="#ffffff" if active else C_DIM)
        for gid, btn in self.group_row_buttons.items():
            active = self.group_id == gid
            btn.configure(
                bg=C_ACCENT if active else C_CARD,
                fg="#ffffff" if active else C_DIM,
                font=("Microsoft YaHei UI", 9, "bold" if active else "normal"),
            )

    def _select_group(self, key: int | str) -> None:
        """切换分组视图：'all' / 'ungrouped' / 分组 id。"""
        self._close_group_menu()  # 视图变化，先收起卡片分组菜单
        if key == "all":
            self.group_id = None
            self.show_ungrouped = False
        elif key == "ungrouped":
            self.group_id = None
            self.show_ungrouped = True
        else:
            self.group_id = int(key)
            self.show_ungrouped = False
        self._paint_group_button_styles()
        self._cancel_pending_delete()  # 视图变化，作废未确认的删除
        self._fingerprint = None
        self._load_and_render()

    def _active_group_name(self) -> str | None:
        """当前查看的分组名（不在具体分组视图时返回 None）。"""
        if self.group_id is None:
            return None
        for group in self.groups.get("groups", []):
            if group["id"] == self.group_id:
                return group["name"]
        return None

    def _new_group_clicked(self) -> None:
        """新建分组（询问名称后创建，并直接切到该分组视图）。"""
        name = simpledialog.askstring("新建分组", "分组名称：", parent=self.root)
        if not name or not name.strip():
            return
        try:
            group_id = storage.create_group(name)
        except ValueError as exc:
            messagebox.showwarning("新建分组", str(exc), parent=self.root)
            return
        self._toast(f"🗂 已创建分组「{name.strip()[:30]}」")
        self._select_group(group_id)

    def _on_group_right_click(self, event, group_id: int) -> None:
        """分组按钮右键菜单：重命名 / 删除。"""
        group = storage.get_group(group_id)
        if group is None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"重命名「{group['name']}」", command=lambda: self._rename_group(group_id))
        menu.add_separator()
        menu.add_command(label="删除分组", command=lambda: self._delete_group(group_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _rename_group(self, group_id: int) -> None:
        """重命名分组（预填当前名称）。"""
        group = storage.get_group(group_id)
        if group is None:
            return
        name = simpledialog.askstring(
            "重命名分组", "新的分组名称：", initialvalue=group["name"], parent=self.root
        )
        if not name or not name.strip():
            return
        try:
            storage.rename_group(group_id, name)
        except ValueError as exc:
            messagebox.showwarning("重命名分组", str(exc), parent=self.root)
            return
        self._toast(f"✏️ 已重命名为「{name.strip()[:30]}」")
        self._fingerprint = None
        self._load_and_render()

    def _delete_group(self, group_id: int) -> None:
        """删除分组（二次确认；条目本身不受影响，只是移出分组）。"""
        group = storage.get_group(group_id)
        if group is None:
            return
        confirmed = messagebox.askyesno(
            "删除分组",
            f"确定删除分组「{group['name']}」吗？\n组内 {group['count']} 条记录不会被删除，只是移出分组。",
            parent=self.root,
        )
        if not confirmed:
            return
        storage.delete_group(group_id)
        # 如果正在看这个分组，退回「全部」视图
        if self.group_id == group_id:
            self.group_id = None
            self.show_ungrouped = False
        self._toast(f"🗑 已删除分组「{group['name']}」")
        self._fingerprint = None
        self._load_and_render()

    # ------------------------------------------------------------------
    # 历史上限：未分组内容每周自动清理 + 手动清理
    # ------------------------------------------------------------------

    def _cleanup_clicked(self) -> None:
        """手动清理未分组条目（二次确认，告知将删条数与下次自动清理时间）。"""
        overview = storage.group_overview()
        pending = overview["ungrouped"]
        if pending <= 0:
            self._toast("没有未分组的条目，无需清理")
            return
        confirmed = messagebox.askyesno(
            "清理未分组",
            f"将永久删除 {pending} 条未分组记录（含图片与语义向量），"
            f"已入组的 {overview['total'] - pending} 条不受影响。\n"
            f"{cleanup.next_cleanup_text()}\n确定现在清理吗？",
            parent=self.root,
        )
        if not confirmed:
            return
        result = cleanup.run_cleanup(force=True)
        if result["deleted"] > 0:
            self._group_sig = None
            self._fingerprint = None
            self._load_and_render()
            self._toast(f"🧹 已清理 {result['deleted']} 条未分组记录")
        else:
            self._toast("清理完成，没有可删的条目")

    def _maybe_auto_cleanup(self) -> None:
        """自动清理钩子（刷新链路调用）：到期才真删，删过就刷新界面 + toast。"""
        result = cleanup.maybe_run()
        if result and result.get("deleted", 0) > 0:
            self._group_sig = None
            self._fingerprint = None
            self._load_and_render()
            self._toast(f"🧹 每周清理：已删除 {result['deleted']} 条未分组记录")

    # ------------------------------------------------------------------
    # 数据加载（关键词 + 语义混合 + 分组过滤）
    # ------------------------------------------------------------------

    def _current_query(self) -> str:
        """取当前搜索词（占位符状态视为空）。"""
        if self._placeholder_active:
            return ""
        return self.search_var.get().strip()

    def _load_items_sync(self, q: str) -> list[dict]:
        """关键词/列表路径：纯本地 SQLite，同步执行无感知；遵守分组视图过滤。"""
        group_id, grouped = None, "all"
        if self.show_ungrouped:
            grouped = "ungrouped"
        elif self.group_id is not None:
            group_id = self.group_id
        if q:
            rows = storage.list_items(
                q=q, limit=LIST_LIMIT, content_type=self.type_filter,
                group_id=group_id, grouped=grouped,
            )
        else:
            rows = storage.list_items(
                limit=LIST_LIMIT, content_type=self.type_filter,
                group_id=group_id, grouped=grouped,
            )
        return self._attach_group_names(rows)

    def _attach_group_names(self, rows: list[dict]) -> list[dict]:
        """给每行挂上 group_names（卡片显示分组徽章、语义路径本地过滤用）。"""
        if not rows:
            return rows
        names_by_id = storage.group_names_by_ids([row["id"] for row in rows])
        for row in rows:
            row["group_names"] = names_by_id.get(row["id"], [])
        return rows

    def _semantic_rows(self, q: str) -> list[dict]:
        """语义检索：查询词向量化后按余弦相似度排序；失败返回空列表（降级关键词）。

        查询向量带缓存：同一个查询词（含 5 秒自动刷新重复触发）不会反复请求接口。
        分组视图在这里做本地过滤（语义候选本来就是全量向量里挑的）。
        """
        if not self.ai_configured:
            return []
        if q in self._query_vec_cache:
            query_vector = self._query_vec_cache[q]
        else:
            query_vector = ai_client.embed_text(q) or []
            # 缓存上限 50 条，防止长期使用无限增长
            if len(self._query_vec_cache) >= 50:
                self._query_vec_cache.pop(next(iter(self._query_vec_cache)))
            self._query_vec_cache[q] = query_vector
        if not query_vector:
            return []
        scored: list[tuple[float, int]] = []
        for item_id, vector in storage.load_vectors():
            score = ai_client.cosine_similarity(query_vector, vector)
            if score > 0.0:
                scored.append((score, item_id))
        scored.sort(key=lambda pair: (-pair[0], -pair[1]))
        row_map = storage.find_by_ids([item_id for _, item_id in scored[:LIST_LIMIT]])
        rows = [row_map[item_id] for _, item_id in scored[:LIST_LIMIT] if item_id in row_map]
        # 语义结果同样遵守置顶优先（稳定排序：组内保持余弦相似度顺序）
        rows.sort(key=lambda r: 0 if r["is_pinned"] else 1)
        # 类型筛选
        if self.type_filter != "all":
            rows = [r for r in rows if r["content_type"] == self.type_filter]
        rows = self._attach_group_names(rows)
        return self._filter_rows_by_group_view(rows)

    def _filter_rows_by_group_view(self, rows: list[dict]) -> list[dict]:
        """按当前分组视图本地过滤（语义检索路径用；SQL 路径已在库里过滤）。"""
        if self.show_ungrouped:
            return [r for r in rows if not r.get("group_names")]
        active = self._active_group_name()
        if active is None:
            return rows
        return [r for r in rows if active in (r.get("group_names") or [])]

    def _load_and_render(self) -> None:
        """加载数据并渲染。

        语义检索涉及网络请求，放到后台线程执行，避免接口挂起时 UI 冻结；
        关键词/列表路径是本地 SQLite，同步执行。
        """
        q = self._current_query()
        use_semantic = bool(q) and (
            self.mode == "semantic" or (self.mode == "auto" and self.ai_configured)
        )
        if use_semantic:
            self._toast("🔍 检索中…")
            threading.Thread(
                target=self._semantic_worker, args=(q,), name="clipvault-search", daemon=True
            ).start()
        else:
            self._apply_rows(self._load_items_sync(q))

    def _semantic_worker(self, q: str) -> None:
        """后台检索，完成后回主线程渲染。

        - 智能模式（auto）：关键词（内容/命名/来源）命中排前，语义增量去重补后。
          本地 LIKE 是确定性的——搜命名、搜来源一定命中；语义只做增量补充，
          不会像纯语义那样把「明明有这个词」的条目漏掉（未建向量的老条目尤其明显）；
        - 纯语义模式（semantic）：仅语义结果；失败/无结果时降级关键词。
        """
        try:
            if self.mode == "auto":
                rows = self._merge_smart_results(q)
            else:
                rows = self._semantic_rows(q) or self._load_items_sync(q)
        except Exception:
            rows = self._load_items_sync(q)
        self.root.after(0, self._apply_rows, rows)

    def _merge_smart_results(self, q: str) -> list[dict]:
        """智能检索：本地关键词（内容/命名/来源）在前，语义增量去重追加在后。"""
        merged = self._load_items_sync(q)
        seen = {row["id"] for row in merged}
        for row in self._semantic_rows(q):
            if row["id"] not in seen:
                merged.append(row)
                seen.add(row["id"])
        return merged[:LIST_LIMIT]

    def _apply_rows(self, rows: list[dict]) -> None:
        """主线程渲染入口：数据没变（指纹一致）就跳过重绘。

        指纹纳入分组归属：分组变化（加入/移出/改名）必须触发重绘，
        否则卡片上的分组徽章和分组栏计数会停在旧状态。
        """
        self.items = rows
        fingerprint = "|".join(
            f"{r['id']}:{r['is_pinned']}:{r['category'] or ''}:{r['content_type']}:"
            f"{r.get('title') or ''}:{','.join(r.get('group_names') or [])}"
            for r in self.items
        )
        if fingerprint == self._fingerprint:
            return
        self._fingerprint = fingerprint
        self._render()

    def _refresh_hover_from_pointer(self) -> None:
        """按当前鼠标位置重算悬停卡片。

        置顶/删除/保存后卡片会重绘，若把 hovered_id 直接清空，按钮门控失效，
        用户原地点按钮会退化成「复制卡片」——所以按指针真实位置恢复。
        """
        try:
            px = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
            py = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
        except Exception:
            self.hovered_id = None
            return
        x, y = self.canvas.canvasx(px), self.canvas.canvasy(py)
        self.hovered_id = None
        for item_id, (cx, cy, cw, ch) in self._card_rects.items():
            if cx <= x <= cx + cw and cy <= y <= cy + ch:
                self.hovered_id = item_id
                break

    def _render(self) -> None:
        """清空画布并按数据重绘所有卡片。"""
        self.canvas.delete("all")
        self._card_rects.clear()
        self._img_refs.clear()
        self._rebuild_sidebar()  # 分组栏计数与当前分组列表保持新鲜

        canvas_width = max(self.canvas.winfo_width(), 400)
        card_width = canvas_width - 2 * CARD_MARGIN
        y = CARD_MARGIN

        if not self.items:
            if self.show_ungrouped:
                empty_text = "没有未分组的条目（新内容都在这里出现）"
            elif self.group_id is not None:
                empty_text = "该分组还是空的，悬停卡片点「分组」把条目加进来"
            else:
                empty_text = "暂无剪贴板记录，去复制点内容试试～"
            self.canvas.create_text(
                canvas_width // 2,
                80,
                text=empty_text,
                fill=C_DIM,
                font=("Microsoft YaHei UI", 12),
            )
            self.canvas.configure(scrollregion=(0, 0, canvas_width, 200))
            self.count_var.set("0 条")
            return

        for item in self.items:
            height = self._card_height(item, card_width)
            self._draw_card(item, CARD_MARGIN, y, card_width, height)
            self._card_rects[item["id"]] = (CARD_MARGIN, y, card_width, height)
            y += height + CARD_PAD

        total = y - CARD_PAD + CARD_MARGIN
        self.canvas.configure(scrollregion=(0, 0, canvas_width, total))
        self.count_var.set(f"{len(self.items)} 条")

    def _card_height(self, item: dict, width: int) -> int:
        """估算卡片高度：图片卡固定，文本卡按行数估算；有名称行另加一行。"""
        extra = 22 if item.get("title") else 0
        if item["content_type"] == "image":
            return THUMB_SIZE + META_HEIGHT + 16 + extra
        text = item.get("text_content") or ""
        chars_per_line = max(20, width // 13)  # 中文约 13px/字
        lines = min(6, max(1, (len(text) + chars_per_line - 1) // chars_per_line))
        return lines * 22 + META_HEIGHT + 16 + extra

    def _draw_card(self, item: dict, x: int, y: int, w: int, h: int) -> None:
        """画单张卡片（背景/内容/元信息/悬停按钮）。"""
        pinned = bool(item["is_pinned"])
        hovered = self.hovered_id == item["id"]

        if pinned:
            bg = C_CARD_PINNED
        elif hovered:
            bg = C_CARD_HOVER
        else:
            bg = C_CARD

        # 卡片背景 + 边框
        self.canvas.create_rectangle(
            x,
            y,
            x + w,
            y + h,
            fill=bg,
            outline=C_BORDER,
            width=1,
            tags=("card", f"card-{item['id']}"),
        )
        # 置顶左侧强调条
        if pinned:
            self.canvas.create_rectangle(
                x,
                y,
                x + 4,
                y + h,
                fill=C_PINNED_BAR,
                outline=C_PINNED_BAR,
                tags=("card", f"card-{item['id']}"),
            )

        meta_y = y + h - META_HEIGHT - 4

        # —— 名称行（用户在编辑窗命名后显示，主题色加粗） ——
        title = item.get("title")
        content_y = y + 8
        if title:
            self.canvas.create_text(
                x + 12,
                content_y,
                text=title,
                fill=C_ACCENT,
                font=("Microsoft YaHei UI", 11, "bold"),
                width=w - 24,
                anchor=tk.NW,
                tags=("card", f"card-{item['id']}"),
            )
            content_y += 22

        # —— 内容区 ——
        if item["content_type"] == "image":
            self._draw_thumb(item, x + 10, content_y, THUMB_SIZE)
        else:
            text = item.get("text_content") or ""
            preview = text[:TEXT_PREVIEW_CHARS] + ("…" if len(text) > TEXT_PREVIEW_CHARS else "")
            self.canvas.create_text(
                x + 12,
                content_y,
                text=preview,
                fill=C_TEXT,
                font=("Microsoft YaHei UI", 11),
                width=w - 24,
                anchor=tk.NW,
                tags=("card", f"card-{item['id']}"),
            )

        # —— 元信息行：类型 + 时间 + 分类 + 分组 + 来源 ——
        type_label = "图片" if item["content_type"] == "image" else "文本"
        meta_text = f"{type_label} · {_fmt_time(item['created_at'])}"
        if item.get("category"):
            meta_text += f" · [{item['category']}]"
        group_names = item.get("group_names") or []
        if group_names:
            meta_text += " · " + " ".join(f"#{name}" for name in group_names)
        meta_text += f" · {item.get('source_app') or '未知来源'}"
        self.canvas.create_text(
            x + 12,
            meta_y,
            text=meta_text,
            fill=C_DIM,
            font=("Microsoft YaHei UI", 9),
            anchor=tk.NW,
            tags=("card", f"card-{item['id']}"),
        )

        # —— 悬停时才画操作按钮 ——
        if hovered:
            self._draw_action_buttons(item, x, y, w)

    def _draw_thumb(self, item: dict, x: int, y: int, size: int) -> None:
        """画图片缩略图（文件缺失时画占位框）。"""
        rel = item.get("thumbnail_path") or item.get("image_path")
        if not rel:
            self.canvas.create_rectangle(
                x,
                y,
                x + size,
                y + size,
                fill=C_CARD_HOVER,
                outline=C_BORDER,
                tags=("card", f"card-{item['id']}"),
            )
            self.canvas.create_text(
                x + size // 2,
                y + size // 2,
                text="图片缺失",
                fill=C_DIM,
                font=("Microsoft YaHei UI", 10),
                tags=("card", f"card-{item['id']}"),
            )
            return
        path = (storage.DATA_DIR / rel).resolve()
        if not path.is_file():
            self.canvas.create_rectangle(
                x,
                y,
                x + size,
                y + size,
                fill=C_CARD_HOVER,
                outline=C_BORDER,
                tags=("card", f"card-{item['id']}"),
            )
            self.canvas.create_text(
                x + size // 2,
                y + size // 2,
                text="图片缺失",
                fill=C_DIM,
                font=("Microsoft YaHei UI", 10),
                tags=("card", f"card-{item['id']}"),
            )
            return
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((size, size))
                photo = ImageTk.PhotoImage(im)
            self._img_refs.append(photo)  # 必须保引用，否则被 GC 后画布空白
            self.canvas.create_image(
                x, y, image=photo, anchor=tk.NW, tags=("card", f"card-{item['id']}")
            )
        except Exception:
            self.canvas.create_rectangle(
                x,
                y,
                x + size,
                y + size,
                fill=C_CARD_HOVER,
                outline=C_BORDER,
                tags=("card", f"card-{item['id']}"),
            )

    def _draw_action_buttons(self, item: dict, card_x: int, card_y: int, card_w: int) -> None:
        """在悬停卡片右上角画「分组 / 置顶 / 编辑 / 删除」按钮。"""
        btn_w, btn_h = ACTION_BTN_W, BTN_HEIGHT
        gap = ACTION_BTN_GAP
        x1 = card_x + card_w - btn_w * 4 - gap * 3 - 8  # 分组
        x2 = x1 + btn_w + gap  # 置顶
        x3 = x2 + btn_w + gap  # 编辑
        x4 = x3 + btn_w + gap  # 删除
        y = card_y + 8

        pinned = bool(item["is_pinned"])
        pin_text = "取消置顶" if pinned else "置顶"
        del_pending = self.pending_delete_id == item["id"]
        del_text = "确认删除" if del_pending else "删除"

        def draw_btn(bx: int, tag: str, text: str, bg: str, fg: str) -> None:
            self.canvas.create_rectangle(
                bx,
                y,
                bx + btn_w,
                y + btn_h,
                fill=bg,
                outline=bg,
                tags=("action", f"{tag}-{item['id']}"),
            )
            self.canvas.create_text(
                bx + btn_w // 2,
                y + btn_h // 2,
                text=text,
                fill=fg,
                font=("Microsoft YaHei UI", 9),
                tags=("action", f"{tag}-{item['id']}"),
            )

        draw_btn(x1, "group", "分组", C_CARD, C_TEXT)
        draw_btn(x2, "pin", pin_text, C_ACCENT, "#ffffff")
        draw_btn(x3, "edit", "编辑", C_CARD, C_TEXT)
        draw_btn(
            x4,
            "del",
            del_text,
            C_DANGER if del_pending else C_CARD,
            "#ffffff" if del_pending else C_TEXT,
        )

    # ------------------------------------------------------------------
    # 画布事件
    # ------------------------------------------------------------------

    def _hit_test(self, event) -> tuple[str | None, int | None]:
        """把画布坐标上的点击解析成
        ('action:group'|'action:pin'|'action:edit'|'action:del'|'card', item_id)。"""
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        # 先查悬停按钮（小区域优先）
        for item_id, (cx, cy, cw, ch) in self._card_rects.items():
            if not (cx <= x <= cx + cw and cy <= y <= cy + ch):
                continue
            btn_w, btn_h, gap = ACTION_BTN_W, BTN_HEIGHT, ACTION_BTN_GAP
            bx1 = cx + cw - btn_w * 4 - gap * 3 - 8  # 分组
            bx2 = bx1 + btn_w + gap  # 置顶
            bx3 = bx2 + btn_w + gap  # 编辑
            bx4 = bx3 + btn_w + gap  # 删除
            by = cy + 8
            if self.hovered_id == item_id:
                if bx1 <= x <= bx1 + btn_w and by <= y <= by + btn_h:
                    return ("action:group", item_id)
                if bx2 <= x <= bx2 + btn_w and by <= y <= by + btn_h:
                    return ("action:pin", item_id)
                if bx3 <= x <= bx3 + btn_w and by <= y <= by + btn_h:
                    return ("action:edit", item_id)
                if bx4 <= x <= bx4 + btn_w and by <= y <= by + btn_h:
                    return ("action:del", item_id)
            return ("card", item_id)
        return (None, None)

    def _on_click(self, event) -> None:
        kind, item_id = self._hit_test(event)
        if kind == "action:group" and item_id is not None:
            self._open_group_menu(event, item_id)
        elif kind == "action:pin" and item_id is not None:
            self._toggle_pin(item_id)
        elif kind == "action:edit" and item_id is not None:
            self.open_editor(item_id)
        elif kind == "action:del" and item_id is not None:
            self._handle_delete_click(item_id)
        elif kind == "card" and item_id is not None:
            self._copy_item(item_id)

    def _on_motion(self, event) -> None:
        """悬停高亮：只在其变化时重绘，避免拖动鼠标疯狂重画。"""
        kind, item_id = self._hit_test(event)
        new_hover = (
            item_id
            if kind in ("card", "action:group", "action:pin", "action:edit", "action:del")
            else None
        )
        if new_hover != self.hovered_id:
            self.hovered_id = new_hover
            self._render()

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _on_canvas_resize(self, event) -> None:
        """窗口宽度变化后重新布局（防抖：宽度确实变了才重绘）。"""
        new_width = event.width
        if getattr(self, "_last_canvas_width", None) != new_width:
            self._last_canvas_width = new_width
            self._render()

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def _copy_item(self, item_id: int) -> None:
        """把条目写回系统剪贴板。"""
        item = storage.get_item(item_id)
        if item is None:
            return
        try:
            if item["content_type"] == "text":
                clipwriter.set_clipboard_text(item.get("text_content") or "")
            else:
                rel = item.get("image_path")
                if not rel:
                    raise FileNotFoundError("原图文件不存在（可能已被清理）")
                clipwriter.set_clipboard_image((storage.DATA_DIR / rel).resolve())
            self._toast("✅ 已复制到剪贴板（可直接粘贴到 QQ/微信）")
        except clipwriter.ClipboardBusyError as exc:
            self._toast(f"⚠️ {exc}", error=True)
        except Exception as exc:
            self._toast(f"❌ 复制失败：{exc}", error=True)

    def _toggle_pin(self, item_id: int) -> None:
        item = storage.get_item(item_id)
        if item is None:
            return
        storage.update_pin(item_id, not bool(item["is_pinned"]))
        self._fingerprint = None  # 强制重绘（排序会变）
        # 先按指针位置恢复悬停，再重绘：否则按钮门控失效，原地点按钮会退化成复制
        self._refresh_hover_from_pointer()
        self._load_and_render()
        self._toast("📌 已置顶" if not item["is_pinned"] else "已取消置顶")

    def _handle_delete_click(self, item_id: int) -> None:
        """删除两步确认：第一次进入确认态，3 秒内再点才真删。"""
        if self.pending_delete_id == item_id:
            self._cancel_pending_delete()
            self._delete_item(item_id)
            return
        self._cancel_pending_delete()
        self.pending_delete_id = item_id
        self._render()
        self._pending_delete_timer = self.root.after(DELETE_CONFIRM_MS, self._cancel_pending_delete)

    def _cancel_pending_delete(self) -> None:
        if self._pending_delete_timer is not None:
            self.root.after_cancel(self._pending_delete_timer)
            self._pending_delete_timer = None
        if self.pending_delete_id is not None:
            self.pending_delete_id = None
            self._render()

    def _delete_item(self, item_id: int) -> None:
        """删除条目：行 + 向量 + 图片文件。"""
        paths = storage.delete_item(item_id)
        for rel in paths:
            path = (storage.DATA_DIR / rel).resolve()
            # 只删数据目录内的文件，防路径穿越
            if path.is_file() and storage.IMAGE_DIR in path.parents:
                path.unlink()
        storage.set_item_groups(item_id, [])  # 连带清掉分组成员关系，不留孤儿行
        self._fingerprint = None
        # 先按指针位置恢复悬停再重绘，避免按钮门控失效
        self._refresh_hover_from_pointer()
        self._load_and_render()
        self._toast("🗑 已删除")

    # ------------------------------------------------------------------
    # 卡片「分组」成员菜单：勾选加入/移出 + 现场新建 + ✨AI 建议
    # ------------------------------------------------------------------

    def _open_group_menu(self, event, item_id: int) -> None:
        """在卡片「分组」按钮下方弹出成员菜单（模态 Toplevel，勾选即时生效）。"""
        # 模态互斥：设置窗/编辑窗开着时先提前面，避免两个模态窗抢 grab
        if getattr(self, "_settings_win", None) is not None:
            self._settings_win.lift()
            self._settings_win.focus_set()
            self._toast("请先关闭 AI 设置窗")
            return
        if getattr(self, "_editor_win", None) is not None:
            self._editor_win.lift()
            self._editor_win.focus_set()
            self._toast("请先关闭编辑窗")
            return
        self._close_group_menu()

        item = storage.get_item(item_id)
        if item is None:
            return
        if item["content_type"] == "image":
            header = "🖼 图片条目 · 选择分组"
        else:
            preview = " ".join((item.get("title") or item.get("text_content") or "").split())
            if len(preview) > 22:
                preview = preview[:22] + "…"
            header = f"📄 {preview}" if preview else "📄 选择分组"

        win = tk.Toplevel(self.root)
        win.title("分组")
        win.configure(bg=C_BG)
        win.transient(self.root)
        win.resizable(False, False)

        tk.Label(
            win,
            text=header,
            bg=C_BG,
            fg=C_DIM,
            font=("Microsoft YaHei UI", 9),
            wraplength=220,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(12, 6), sticky=tk.W)

        current = set(storage.item_group_ids(item_id))
        menu_vars: dict[int, tk.BooleanVar] = {}
        if self.groups.get("groups"):
            for group in self.groups["groups"]:
                var = tk.BooleanVar(value=group["id"] in current)
                menu_vars[group["id"]] = var
                tk.Checkbutton(
                    win,
                    text=group["name"],
                    variable=var,
                    command=lambda gid=group["id"], v=var: self._toggle_item_group(item_id, gid, v.get()),
                    bg=C_BG,
                    fg=C_TEXT,
                    selectcolor=C_CARD,
                    activebackground=C_BG,
                    activeforeground=C_TEXT,
                    font=("Microsoft YaHei UI", 10),
                    anchor=tk.W,
                    padx=12,
                ).grid(row=len(menu_vars), column=0, columnspan=2, padx=12, pady=1, sticky=tk.W + tk.E)
        else:
            tk.Label(
                win,
                text="（还没有分组，点下面按钮新建）",
                bg=C_BG,
                fg=C_DIM,
                font=("Microsoft YaHei UI", 9),
            ).grid(row=1, column=0, columnspan=2, padx=12, pady=2, sticky=tk.W)

        row = len(menu_vars) + 1 + (0 if menu_vars else 1)  # 空态提示也占一行
        # —— 底部动作：新建 / AI 建议 / 完成 ——
        btn_row = tk.Frame(win, bg=C_BG)
        btn_row.grid(row=row, column=0, columnspan=2, padx=12, pady=(10, 12), sticky=tk.E)

        def make_btn(text, cmd, accent=False, enabled=True):
            return tk.Button(
                btn_row,
                text=text,
                command=cmd,
                bg=C_ACCENT if accent else C_CARD,
                fg="#ffffff" if accent else C_TEXT,
                activebackground=C_CARD_HOVER,
                activeforeground=C_TEXT,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 9),
                padx=10,
                pady=2,
                cursor="hand2",
                state=tk.NORMAL if enabled else tk.DISABLED,
            )

        make_btn("＋ 新建分组…", lambda: self._group_menu_new(item_id)).pack(side=tk.LEFT, padx=(0, 6))
        make_btn(
            "✨ AI 建议",
            lambda: self._group_menu_ai_suggest(item_id),
            enabled=self.ai_configured,
        ).pack(side=tk.LEFT, padx=(0, 6))
        make_btn("完成", self._close_group_menu, accent=True).pack(side=tk.LEFT)

        # 定位到按钮下方（事件坐标是屏幕坐标）
        x = max(0, event.x_root - 60)
        y = event.y_root + BTN_HEIGHT + 6
        win.geometry(f"+{x}+{y}")
        self._group_menu_win = win
        self._group_menu_item = item_id
        self._group_menu_vars = menu_vars
        win.protocol("WM_DELETE_WINDOW", self._close_group_menu)
        win.grab_set()  # 模态：菜单开着期间不操作主窗口，避免悬停重绘错位
        win.focus_set()

    def _close_group_menu(self) -> None:
        """关闭分组成员菜单（释放模态）。"""
        win = getattr(self, "_group_menu_win", None)
        if win is None:
            return
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()
        self._group_menu_win = None

    def _toggle_item_group(self, item_id: int, group_id: int, on: bool) -> None:
        """勾选加入/移出分组（即时写入并刷新列表与分组栏计数）。"""
        if on:
            storage.add_item_to_group(item_id, group_id)
        else:
            storage.remove_item_from_group(item_id, group_id)
        self._group_sig = None  # 计数可能变化，强制刷新分组栏
        self._fingerprint = None
        self._load_and_render()

    def _group_menu_new(self, item_id: int) -> None:
        """成员菜单里现场新建分组，并把这件条目直接加进去。"""
        parent = self._group_menu_win or self.root
        name = simpledialog.askstring("新建分组", "分组名称：", parent=parent)
        if not name or not name.strip():
            return
        try:
            group_id = storage.create_group(name)
        except ValueError as exc:
            messagebox.showwarning("新建分组", str(exc), parent=parent)
            return
        storage.add_item_to_group(item_id, group_id)
        self._close_group_menu()
        self._toast(f"🗂 已加入新分组「{name.strip()[:30]}」")
        self._group_sig = None
        self._fingerprint = None
        self._load_and_render()

    def _group_menu_ai_suggest(self, item_id: int) -> None:
        """AI 建议本条去哪组（后台线程，不阻塞 UI；未配置 AI 自动降级）。"""
        item = storage.get_item(item_id)
        if item is None:
            return
        text = (item.get("text_content") or "").strip()
        if not text:
            self._toast("图片条目不做 AI 建议分组", error=True)
            return
        existing = [g["name"] for g in self.groups.get("groups", [])]
        self._toast("✨ AI 建议中…")

        def _run():
            try:
                name = ai_client.suggest_group(text, existing)
            except Exception:
                name = None
            self.root.after(0, self._apply_ai_group_suggestion, item_id, name)

        threading.Thread(target=_run, name="clipvault-group-suggest", daemon=True).start()

    def _apply_ai_group_suggestion(self, item_id: int, name: str | None) -> None:
        """应用 AI 的单条分组建议（主线程执行）。"""
        if not name:
            self._toast("AI 没给出建议（未配置或接口失败）", error=True)
            return
        # 同名分组直接复用；没有就新建（并发重名时回退查库）
        group_id = next(
            (g["id"] for g in self.groups.get("groups", []) if g["name"] == name), None
        )
        created = False
        if group_id is None:
            try:
                group_id = storage.create_group(name)
                created = True
            except ValueError:
                group_id = next(
                    (g["id"] for g in storage.list_groups() if g["name"] == name), None
                )
        if group_id is None:
            self._toast(f"无法加入分组「{name}」", error=True)
            return
        storage.add_item_to_group(item_id, group_id)
        # 菜单还开着就把对应勾打上，用户能立刻看到结果
        win = self._group_menu_win
        if win is not None and getattr(self, "_group_menu_item", None) == item_id:
            var = self._group_menu_vars.get(group_id)
            if var is not None:
                var.set(True)
        self._toast(f"✨ AI 建议：加入「{name}」" + ("（新分组）" if created else ""))
        self._group_sig = None
        self._fingerprint = None
        self._load_and_render()

    # ------------------------------------------------------------------
    # AI 自动分组：后台分批把未分组条目交给分类模型归组
    # ------------------------------------------------------------------

    def start_auto_group(self) -> None:
        """启动 AI 自动分组（后台线程；完成后回主线程刷新并汇报）。"""
        if not self.ai_configured:
            self._toast("AI 未配置：请先在「AI 设置」里配好 Key", error=True)
            return
        if getattr(self, "_auto_group_running", False):
            self._toast("AI 自动分组正在进行中…")
            return
        self._auto_group_running = True
        self._toast("🤖 AI 自动分组中（后台处理）…")
        threading.Thread(
            target=self._auto_group_worker, name="clipvault-auto-group", daemon=True
        ).start()

    def _run_auto_group(self) -> str:
        """同步执行 AI 自动分组（后台线程调用）；返回汇报文案。

        流程：取未分组文本 -> 分批交给模型分配 -> 新建缺失分组 -> 写入成员关系。
        任一批次失败只丢该批，其余批次结果仍然生效。
        """
        rows = storage.ungrouped_text_items(500)
        if not rows:
            return "没有未分组的条目"
        payload = [(row["id"], row["text_content"] or "") for row in rows]
        existing = [g["name"] for g in storage.list_groups()]
        mapping = ai_client.assign_groups(payload, existing)
        if not mapping:
            return "AI 未给出分组建议（未配置或接口失败）"
        name_to_id = {g["name"]: g["id"] for g in storage.list_groups()}
        created = assigned = failed = 0
        for item_id, group_name in mapping.items():
            group_id = name_to_id.get(group_name)
            if group_id is None:
                try:
                    group_id = storage.create_group(group_name)
                    name_to_id[group_name] = group_id
                    created += 1
                except ValueError:
                    # 并发/重名：回退查库；仍找不到才记失败
                    group_id = next(
                        (g["id"] for g in storage.list_groups() if g["name"] == group_name), None
                    )
                    if group_id is None:
                        failed += 1
                        continue
            try:
                storage.add_item_to_group(item_id, group_id)
                assigned += 1
            except Exception:
                failed += 1
        message = f"AI 分组完成：{assigned} 条入组，新建 {created} 个分组"
        if failed:
            message += f"，{failed} 条失败"
        return message

    def _auto_group_worker(self) -> None:
        """后台包装：跑同步逻辑，结果回主线程刷新（tkinter 非线程安全）。"""
        try:
            message = self._run_auto_group()
        except Exception as exc:
            message = f"AI 分组出错：{exc}"
        self.root.after(0, self._finish_auto_group, message)

    def _finish_auto_group(self, message: str) -> None:
        """主线程收尾：刷新列表与分组栏，toast 汇报。"""
        self._auto_group_running = False
        self._group_sig = None
        self._fingerprint = None
        self._load_and_render()
        self._toast(message)

    def _on_type_filter(self, key: str) -> None:
        self.type_filter = key
        for k, btn in self.type_buttons.items():
            btn.configure(bg=C_ACCENT if k == key else C_CARD, fg="#ffffff" if k == key else C_DIM)
        self._cancel_pending_delete()  # 筛选项变化，作废未确认的删除
        self._fingerprint = None
        self._load_and_render()

    def _on_mode(self, key: str) -> None:
        self.mode = key
        for k, btn in self.mode_buttons.items():
            btn.configure(bg=C_ACCENT if k == key else C_CARD, fg="#ffffff" if k == key else C_DIM)
        self._fingerprint = None
        self._load_and_render()

    def _rebuild_mode_buttons(self) -> None:
        """按当前 AI 配置状态重建「检索模式」按钮（设置窗口保存后调用）。"""
        for btn in self.mode_buttons.values():
            btn.destroy()
        self.mode_buttons = {}
        if not self.ai_configured:
            return
        for label, key in (("智能", "auto"), ("关键词", "keyword"), ("语义", "semantic")):
            btn = tk.Button(
                self.mode_frame,
                text=label,
                command=lambda k=key: self._on_mode(k),
                bg=C_ACCENT if key == self.mode else C_CARD,
                fg="#ffffff" if key == self.mode else C_DIM,
                activebackground=C_CARD_HOVER,
                activeforeground=C_TEXT,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 10),
                padx=10,
                pady=2,
                cursor="hand2",
            )
            btn.pack(side=tk.LEFT, padx=(8, 0))
            self.mode_buttons[key] = btn

    # ------------------------------------------------------------------
    # AI 设置窗口（界面直接配置 API，保存到 settings.json 即时生效）
    # ------------------------------------------------------------------

    #: 普通文本设置项：（标签, 配置键, 控件类型）
    _SETTINGS_FIELDS: tuple[tuple[str, str, str], ...] = (
        ("API Key", "CLIPVAULT_AI_API_KEY", "secret"),
        ("接口地址 Base URL", "CLIPVAULT_AI_BASE_URL", "text"),
        ("候选分类（逗号分隔）", "CLIPVAULT_AI_CATEGORIES", "text"),
        ("请求超时（秒）", "CLIPVAULT_AI_TIMEOUT", "text"),
    )

    #: 模型设置项：下拉框，选项由「拉取模型」填充
    _MODEL_FIELDS: tuple[tuple[str, str], ...] = (
        ("分类模型", "CLIPVAULT_AI_CHAT_MODEL"),
        ("向量模型", "CLIPVAULT_AI_EMBED_MODEL"),
    )

    #: 设置项的默认展示值（与 ai_client 的常量保持一致）
    _SETTINGS_DEFAULTS = {
        "CLIPVAULT_AI_API_KEY": "",
        "CLIPVAULT_AI_BASE_URL": "https://api.openai.com/v1",
        "CLIPVAULT_AI_CHAT_MODEL": "gpt-4o-mini",
        "CLIPVAULT_AI_EMBED_MODEL": "text-embedding-3-small",
        # 注意：必须用英文逗号 —— ai_client.get_categories() 按 "," 分割，
        # 用中文顿号会把全部分类黏成一个字符串
        "CLIPVAULT_AI_CATEGORIES": ",".join(ai_client.DEFAULT_CATEGORIES),
        "CLIPVAULT_AI_TIMEOUT": "10",
    }

    def _make_dark_combobox_style(self) -> None:
        """给 ttk.Combobox 配一套深色样式（clam 主题支持自定义 fieldbackground）。"""
        try:
            style = ttk.Style()
            style.theme_use("clam")
            style.configure(
                "Dark.TCombobox",
                fieldbackground=C_CARD,
                background=C_CARD,
                foreground=C_TEXT,
                arrowcolor=C_TEXT,
                borderwidth=1,
            )
            style.map("Dark.TCombobox", fieldbackground=[("readonly", C_CARD)])
        except Exception:
            pass  # 样式失败不碍事，用默认外观

    def open_settings(self) -> None:
        """打开 AI 设置窗口（模态）；已打开则提到最前。"""
        existing = getattr(self, "_settings_win", None)
        if existing is not None:
            existing.lift()
            existing.focus_set()
            return
        # 模态互斥：编辑窗开着时先把编辑窗提到前面
        if getattr(self, "_editor_win", None) is not None:
            self._editor_win.lift()
            self._editor_win.focus_set()
            self._toast("请先关闭编辑窗")
            return

        win = tk.Toplevel(self.root)
        win.title("AI 设置")
        win.configure(bg=C_BG)
        win.transient(self.root)
        win.resizable(False, False)
        self._make_dark_combobox_style()

        entries: dict[str, tk.StringVar] = {}
        show_key = tk.BooleanVar(value=False)

        # —— 顶部说明 ——
        tk.Label(
            win,
            text="AI 用于自动分类与语义搜索；不配置则自动使用关键词搜索。\n"
            "配置保存在 clipboard_data/settings.json，仅存在本机。",
            bg=C_BG,
            fg=C_DIM,
            font=("Microsoft YaHei UI", 9),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=3, padx=14, pady=(14, 8), sticky=tk.W)

        # —— 启用开关 ——
        enabled_var = tk.BooleanVar(
            value=config.get_setting("CLIPVAULT_AI_ENABLED", "1").lower()
            not in ("0", "false", "no")
        )
        tk.Checkbutton(
            win,
            text="启用 AI 功能",
            variable=enabled_var,
            bg=C_BG,
            fg=C_TEXT,
            selectcolor=C_CARD,
            activebackground=C_BG,
            activeforeground=C_TEXT,
            font=("Microsoft YaHei UI", 10),
        ).grid(row=1, column=0, columnspan=3, padx=14, pady=4, sticky=tk.W)

        # —— 厂商预设（选中自动填 Base URL 与默认模型） ——
        tk.Label(win, text="厂商预设", bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 10)).grid(
            row=2, column=0, padx=(14, 6), pady=4, sticky=tk.W
        )
        current_base = config.get_setting("CLIPVAULT_AI_BASE_URL", "").strip().rstrip("/")
        provider_names = list(ai_client.PROVIDERS.keys())
        current_provider = "自定义"
        for name, preset in ai_client.PROVIDERS.items():
            if preset["base"] and preset["base"].rstrip("/") == current_base:
                current_provider = name
                break
        provider_var = tk.StringVar(value=current_provider)
        self._settings_provider_var = provider_var  # 恢复默认时要回退这个下拉
        provider_box = ttk.Combobox(
            win,
            textvariable=provider_var,
            values=provider_names,
            width=40,
            state="readonly",
            style="Dark.TCombobox",
            font=("Microsoft YaHei UI", 10),
        )
        provider_box.grid(row=2, column=1, padx=4, pady=4, sticky=tk.W)
        provider_box.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._settings_apply_provider(provider_var, entries),
        )

        # —— 普通字段 ——
        row = 3
        for label, key, kind in self._SETTINGS_FIELDS:
            tk.Label(win, text=label, bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 10)).grid(
                row=row, column=0, padx=(14, 6), pady=4, sticky=tk.W
            )
            var = tk.StringVar(value=config.get_setting(key, self._SETTINGS_DEFAULTS.get(key, "")))
            entries[key] = var
            entry = tk.Entry(
                win,
                textvariable=var,
                width=42,
                show="*" if kind == "secret" else "",
                bg=C_CARD,
                fg=C_TEXT,
                insertbackground=C_TEXT,
                highlightbackground=C_BORDER,
                highlightcolor=C_ACCENT,
                highlightthickness=1,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 10),
            )
            entry.grid(row=row, column=1, padx=4, pady=4)
            if kind == "secret":
                tk.Checkbutton(
                    win,
                    text="显示",
                    variable=show_key,
                    command=lambda e=entry: e.configure(show="" if show_key.get() else "*"),
                    bg=C_BG,
                    fg=C_DIM,
                    selectcolor=C_CARD,
                    activebackground=C_BG,
                    activeforeground=C_TEXT,
                    font=("Microsoft YaHei UI", 9),
                ).grid(row=row, column=2, padx=(0, 14), sticky=tk.W)
            if key == "CLIPVAULT_AI_BASE_URL":
                # Base URL 行尾放「拉取模型」按钮
                fetch_btn = tk.Button(
                    win,
                    text="拉取模型",
                    command=lambda: self._settings_fetch_models(win, entries, provider_var),
                    bg=C_ACCENT,
                    fg="#ffffff",
                    activebackground=C_CARD_HOVER,
                    activeforeground=C_TEXT,
                    relief=tk.FLAT,
                    font=("Microsoft YaHei UI", 9),
                    padx=8,
                    pady=1,
                    cursor="hand2",
                )
                fetch_btn.grid(row=row, column=2, padx=(0, 14), sticky=tk.W)
            row += 1

        # —— 模型字段（下拉，选项来自拉取结果；也可手输） ——
        model_boxes: dict[str, ttk.Combobox] = {}
        for label, key in self._MODEL_FIELDS:
            tk.Label(win, text=label, bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 10)).grid(
                row=row, column=0, padx=(14, 6), pady=4, sticky=tk.W
            )
            var = tk.StringVar(value=config.get_setting(key, self._SETTINGS_DEFAULTS.get(key, "")))
            entries[key] = var
            box = ttk.Combobox(
                win,
                textvariable=var,
                width=42,
                style="Dark.TCombobox",
                font=("Microsoft YaHei UI", 10),
            )
            box.grid(row=row, column=1, padx=4, pady=4)
            model_boxes[key] = box
            row += 1

        # —— 按钮行 ——
        btn_row = tk.Frame(win, bg=C_BG)
        btn_row.grid(row=row, column=0, columnspan=3, padx=14, pady=(12, 14), sticky=tk.E)

        def make_btn(text, cmd, danger=False, accent=False):
            return tk.Button(
                btn_row,
                text=text,
                command=cmd,
                bg=C_DANGER if danger else (C_ACCENT if accent else C_CARD),
                fg="#ffffff" if (danger or accent) else C_TEXT,
                activebackground=C_CARD_HOVER,
                activeforeground=C_TEXT,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 10),
                padx=12,
                pady=3,
                cursor="hand2",
            )

        make_btn("测试连接", lambda: self._settings_test(win, entries, enabled_var)).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        make_btn(
            "恢复默认", lambda: self._settings_reset(win, entries, enabled_var), danger=True
        ).pack(side=tk.LEFT, padx=(0, 8))
        make_btn("保存", lambda: self._settings_save(win, entries, enabled_var), accent=True).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        make_btn("完成", self._close_settings).pack(side=tk.LEFT)

        self._settings_entries = entries
        self._settings_model_boxes = model_boxes
        self._settings_enabled = enabled_var
        self._settings_win = win
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        win.grab_set()  # 模态：设置窗口打开期间不操作主窗口
        win.focus_set()

    def _settings_apply_provider(
        self, provider_var: tk.StringVar, entries: dict[str, tk.StringVar]
    ) -> None:
        """选中厂商预设：自动填 Base URL 与默认模型（自定义不清空）。"""
        preset = ai_client.PROVIDERS.get(provider_var.get())
        if not preset:
            return
        if preset["base"]:
            entries["CLIPVAULT_AI_BASE_URL"].set(preset["base"])
        if preset["chat"]:
            entries["CLIPVAULT_AI_CHAT_MODEL"].set(preset["chat"])
        if preset["embed"]:
            entries["CLIPVAULT_AI_EMBED_MODEL"].set(preset["embed"])
        else:
            # 该厂商没有向量接口：清空残留值，避免语义检索静默失效
            entries["CLIPVAULT_AI_EMBED_MODEL"].set("")
            self._toast(f"「{provider_var.get()}」无向量接口，语义搜索将不可用")
            return
        self._toast(f"已应用「{provider_var.get()}」预设，填好 Key 后可点「拉取模型」")

    def _settings_fetch_models(
        self, win, entries: dict[str, tk.StringVar], provider_var: tk.StringVar
    ) -> None:
        """后台拉取模型列表（不阻塞 UI），回填到两个模型下拉框。"""
        # 先把当前界面值存盘，ai_client 才能拿到最新的 URL/Key
        enabled_var = getattr(self, "_settings_enabled", None)
        if enabled_var is None:
            enabled_var = tk.BooleanVar(
                value=config.get_setting("CLIPVAULT_AI_ENABLED", "1").lower()
                not in ("0", "false", "no")
            )
        self._settings_save(win, entries, enabled_var)
        if not config.get_setting("CLIPVAULT_AI_BASE_URL", "").strip():
            messagebox.showwarning("拉取模型", "请先填写接口地址 Base URL。", parent=win)
            return
        self._toast("正在拉取模型列表…")

        def _run():
            # try/finally 保证无论成功失败都恢复 UI（否则 toast 永远停在「正在拉取」）
            try:
                models = ai_client.fetch_models()
            except Exception as exc:
                models = []
                self.root.after(0, self._toast, f"拉取模型出错：{exc}", True)
            finally:
                self.root.after(0, self._fill_model_boxes, models)

        threading.Thread(target=_run, name="clipvault-fetch-models", daemon=True).start()

    def _fill_model_boxes(self, models: list[str]) -> None:
        """把拉取到的模型列表填进模型下拉框（保留当前已选值）。"""
        if getattr(self, "_settings_win", None) is None:
            return
        boxes = getattr(self, "_settings_model_boxes", {})
        if models:
            for box in boxes.values():
                box.configure(values=models)
            self._toast(f"✅ 拉到 {len(models)} 个模型，已在下方下拉框中选择")
        else:
            self._toast("未拉到模型：请检查 URL / Key / 网络", error=True)

    def _settings_collect(
        self, entries: dict[str, tk.StringVar], enabled_var: tk.BooleanVar
    ) -> dict[str, str]:
        """从界面收集设置值。"""
        values = {"CLIPVAULT_AI_ENABLED": "1" if enabled_var.get() else "0"}
        for key, var in entries.items():
            values[key] = var.get().strip()
        return values

    def _settings_save(
        self, win, entries: dict[str, tk.StringVar], enabled_var: tk.BooleanVar
    ) -> None:
        """保存设置到 settings.json，并刷新 AI 相关界面状态。"""
        config.save_settings(self._settings_collect(entries, enabled_var))
        self._after_settings_changed()

    def _settings_reset(
        self, win, entries: dict[str, tk.StringVar], enabled_var: tk.BooleanVar
    ) -> None:
        """清除 GUI 覆盖，恢复「环境变量 + 默认值」并刷新界面值。"""
        config.clear_settings()
        enabled_var.set(
            config.get_setting("CLIPVAULT_AI_ENABLED", "1").lower() not in ("0", "false", "no")
        )
        for key, var in entries.items():
            var.set(config.get_setting(key, self._SETTINGS_DEFAULTS.get(key, "")))
        # 厂商下拉回退到「按当前 Base URL 识别」的结果；模型下拉清空拉取结果
        provider_var = getattr(self, "_settings_provider_var", None)
        if provider_var is not None:
            current_base = config.get_setting("CLIPVAULT_AI_BASE_URL", "").strip().rstrip("/")
            matched = "自定义"
            for name, preset in ai_client.PROVIDERS.items():
                if preset["base"] and preset["base"].rstrip("/") == current_base:
                    matched = name
                    break
            provider_var.set(matched)
        for box in getattr(self, "_settings_model_boxes", {}).values():
            box.configure(values=())
        self._after_settings_changed()

    def _after_settings_changed(self) -> None:
        """设置变更后的统一刷新：AI 状态、模式按钮、顶部按钮配色、提示。"""
        self.ai_configured = ai_client.is_configured()
        self._rebuild_mode_buttons()
        self.settings_btn.configure(fg=C_ACCENT if self.ai_configured else C_DIM)
        # AI 自动分组按钮跟随 AI 配置状态：未配置时置灰（安全降级，不乱调接口）
        self.auto_group_btn.configure(
            state=tk.NORMAL if self.ai_configured else tk.DISABLED,
            bg=C_ACCENT if self.ai_configured else C_CARD,
            fg="#ffffff" if self.ai_configured else C_DIM,
        )
        # 通知外部（托盘）刷新动态菜单：pystray 菜单只构建一次，需显式 update_menu
        hook = getattr(self, "notify_hook", None)
        if hook is not None:
            try:
                hook()
            except Exception:
                pass
        self._toast(
            "✅ AI 设置已保存，对新内容即时生效"
            if self.ai_configured
            else "AI 已停用（未配置或已关闭），搜索使用关键词模式"
        )

    def _settings_test(
        self, win, entries: dict[str, tk.StringVar], enabled_var: tk.BooleanVar
    ) -> None:
        """先保存当前界面值，再后台做一次连接自检（避免阻塞 UI）。

        自检按当前实际配置选择探测对象（见 ai_client.test_connection）：
        配了向量模型测嵌入，没配（如 DeepSeek）测对话接口 —— 分类与分组
        本来就只用 chat，拿 embedding 的结果判死活会误报「连接失败」。
        """
        self._settings_save(win, entries, enabled_var)
        if not ai_client.is_configured():
            messagebox.showwarning("连接测试", "尚未配置 API Key 或 AI 已停用。", parent=win)
            return
        self._toast("正在测试连接…")

        def _run():
            try:
                ok, message = ai_client.test_connection()
            except Exception as exc:
                ok, message = False, f"连接出错：{exc}"
            self.root.after(0, self._show_test_result, f"{'✅ ' if ok else '❌ '}{message}")

        threading.Thread(target=_run, name="clipvault-ai-test", daemon=True).start()

    def _show_test_result(self, message: str) -> None:
        """在主线程弹出连接测试结果。"""
        if getattr(self, "_settings_win", None) is not None:
            messagebox.showinfo("连接测试", message, parent=self._settings_win)

    def _close_settings(self) -> None:
        """关闭设置窗口（释放模态）。"""
        win = getattr(self, "_settings_win", None)
        if win is None:
            return
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()
        self._settings_win = None

    # ------------------------------------------------------------------
    # 条目编辑：改内容 + 命名
    # ------------------------------------------------------------------

    def open_editor(self, item_id: int) -> None:
        """打开条目编辑窗口：文本条目可改内容+命名，图片条目仅支持命名。"""
        item = storage.get_item(item_id)
        if item is None:
            return
        # 模态互斥：设置窗开着时先把设置窗提到前面，避免两个模态窗抢 grab
        if getattr(self, "_settings_win", None) is not None:
            self._settings_win.lift()
            self._settings_win.focus_set()
            self._toast("请先关闭 AI 设置窗")
            return
        # 已打开则先正常关闭（释放模态抓取），再开新的
        if getattr(self, "_editor_win", None) is not None:
            self._close_editor()

        win = tk.Toplevel(self.root)
        win.title("编辑条目")
        win.configure(bg=C_BG)
        win.transient(self.root)
        win.resizable(False, False)

        is_text = item["content_type"] == "text"
        tk.Label(
            win,
            text=(
                "修改文本内容并给它起个名字；保存后立即生效。"
                if is_text
                else "图片条目不支持改内容，可以给它起个名字。"
            ),
            bg=C_BG,
            fg=C_DIM,
            font=("Microsoft YaHei UI", 9),
        ).grid(row=0, column=0, columnspan=2, padx=14, pady=(14, 8), sticky=tk.W)

        # —— 名称 ——
        tk.Label(win, text="名称", bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 10)).grid(
            row=1, column=0, padx=(14, 6), pady=4, sticky=tk.NW
        )
        title_var = tk.StringVar(value=item.get("title") or "")
        tk.Entry(
            win,
            textvariable=title_var,
            width=46,
            bg=C_CARD,
            fg=C_TEXT,
            insertbackground=C_TEXT,
            highlightbackground=C_BORDER,
            highlightcolor=C_ACCENT,
            highlightthickness=1,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
        ).grid(row=1, column=1, padx=(0, 14), pady=4)

        # —— 内容（仅文本条目可编辑） ——
        content_box = None
        if is_text:
            tk.Label(win, text="内容", bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 10)).grid(
                row=2, column=0, padx=(14, 6), pady=4, sticky=tk.NW
            )
            content_box = tk.Text(
                win,
                width=46,
                height=10,
                bg=C_CARD,
                fg=C_TEXT,
                insertbackground=C_TEXT,
                highlightbackground=C_BORDER,
                highlightcolor=C_ACCENT,
                highlightthickness=1,
                relief=tk.FLAT,
                font=("Microsoft YaHei UI", 10),
                wrap=tk.WORD,
            )
            content_box.insert("1.0", item.get("text_content") or "")
            content_box.grid(row=2, column=1, padx=(0, 14), pady=4)

        # —— 按钮 ——
        btn_row = tk.Frame(win, bg=C_BG)
        btn_row.grid(row=3, column=0, columnspan=2, padx=14, pady=(12, 14), sticky=tk.E)

        def on_save():
            self._save_editor(win, item, is_text, content_box, title_var)

        tk.Button(
            btn_row,
            text="保存",
            command=on_save,
            bg=C_ACCENT,
            fg="#ffffff",
            activebackground=C_CARD_HOVER,
            activeforeground=C_TEXT,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=14,
            pady=3,
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            btn_row,
            text="取消",
            command=self._close_editor,
            bg=C_CARD,
            fg=C_TEXT,
            activebackground=C_CARD_HOVER,
            activeforeground=C_TEXT,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=14,
            pady=3,
            cursor="hand2",
        ).pack(side=tk.LEFT)

        self._editor_win = win
        self._editor_item_id = item_id
        win.protocol("WM_DELETE_WINDOW", self._close_editor)
        win.grab_set()
        win.focus_set()

    def _close_editor(self) -> None:
        """关闭编辑窗口（释放模态）。"""
        win = getattr(self, "_editor_win", None)
        if win is None:
            return
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()
        self._editor_win = None

    def _save_editor(self, win, item: dict, is_text: bool, content_box, title_var) -> None:
        """保存编辑窗：写回内容/名称，冲突或空值弹窗不关窗，成功则刷新列表。

        抽出为方法以便测试驱动；content_box 只需提供 .get("1.0", END)。
        """
        try:
            content_changed = False
            new_text = ""
            if is_text and content_box is not None:
                new_text = content_box.get("1.0", tk.END).strip()
                if not new_text and (item.get("text_content") or "").strip():
                    # 清空内容不是「无操作」：明确拒绝并提示，不静默保存
                    messagebox.showwarning("无法保存", "内容不能为空", parent=win)
                    return
                if new_text and new_text != (item.get("text_content") or "").strip():
                    storage.update_item_content(item["id"], new_text)
                    content_changed = True
            new_title = title_var.get().strip()
            if new_title != (item.get("title") or ""):
                storage.update_item_title(item["id"], new_title)
        except storage.ContentConflictError as exc:
            messagebox.showwarning("无法保存", str(exc), parent=win)
            return
        except ValueError as exc:
            messagebox.showwarning("无法保存", str(exc), parent=win)
            return
        # 内容改了：旧分类/向量已失效，重新入队 AI 分析（未配置 AI 时自动跳过）
        if content_changed:
            ai_client.enqueue_analysis(item["id"], new_text)
        # 内容/名称可能变化：强制刷新列表（先恢复悬停再重绘）
        self._fingerprint = None
        self._refresh_hover_from_pointer()
        self._load_and_render()
        self._toast("✅ 已保存修改")
        self._close_editor()

    def _on_search_changed(self, *_args) -> None:
        """搜索防抖：停止输入 300ms 后才查询。"""
        if self._search_timer is not None:
            self.root.after_cancel(self._search_timer)
        self._search_timer = self.root.after(SEARCH_DEBOUNCE_MS, self._apply_search)

    def _apply_search(self) -> None:
        self._fingerprint = None
        self._load_and_render()

    # ------------------------------------------------------------------
    # 提示与自动刷新
    # ------------------------------------------------------------------

    def _toast(self, message: str, error: bool = False) -> None:
        """底部浮动提示，2 秒后消失（tkinter 只接受 6 位 hex 色值）。"""
        self.toast_var.set(message)
        self.toast_label.configure(bg="#b23b3b" if error else "#2e7d4f")
        self.toast_label.place(relx=0.5, rely=0.94, anchor=tk.CENTER)
        if getattr(self, "_toast_timer", None) is not None:
            self.root.after_cancel(self._toast_timer)
        self._toast_timer = self.root.after(2000, self.toast_label.place_forget)

    def stop_refresh(self) -> None:
        """停止自动刷新链路与待触发的定时器（窗口复用/拆卸时调用）。"""
        self._refresh_stopped = True
        for attr in ("_pending_delete_timer", "_search_timer", "_toast_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    self.root.after_cancel(timer)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._close_group_menu()  # 拆卸窗口时顺手收起模态菜单，释放 grab

    def _schedule_refresh(self) -> None:
        """每 5 秒拉一次数据；指纹不变不重绘（不闪）。

        顺带跑「每周清理未分组」的到期检查（cleanup 内部按小时节流，
        状态文件读取足够廉价，不会给 5 秒刷新增加可感知开销）。
        """
        if self._refresh_stopped:
            return
        self._maybe_auto_cleanup()
        self._load_and_render()
        self.root.after(AUTO_REFRESH_MS, self._schedule_refresh)

    # ------------------------------------------------------------------
    # 自动更新：启动后台检查 GitHub Release -> 下载 -> 确认后热替换重启
    # ------------------------------------------------------------------

    def start_update_check(self, manual: bool = False) -> None:
        """启动一次更新检查（守护线程，不阻塞界面）。

        manual=True 来自用户手动触发（托盘「检查更新」）：没有新版本 /
        检查失败都会给明确反馈；自动检查（启动时）只在新版本时才打扰用户。
        """
        if getattr(self, "_update_checking", False):
            if manual:
                self._toast("正在检查更新…")
            return
        self._update_checking = True
        if manual:
            self._toast("正在检查更新…")
        updater.start_background_check(
            lambda info: self.root.after(0, self._on_update_checked, info, manual)
        )

    def _on_update_checked(self, info: dict | None, manual: bool) -> None:
        """检查完成（主线程）：无新版本仅手动时提示；有新版本则后台下载。"""
        self._update_checking = False
        if info is None:
            if manual:
                self._toast(f"✅ 已是最新版本 v{updater.current_version()}", error=not manual)
            return
        self._pending_update = info
        self._toast(f"🔆 发现新版本 v{info['version']}，正在后台下载…")

        def _run():
            cached = updater._cached_zip(info["version"])
            zip_path = cached or updater.download_update(info["zip_url"], info["version"])
            self.root.after(0, self._on_update_downloaded, zip_path, info)

        threading.Thread(target=_run, name="clipvault-update-download", daemon=True).start()

    def _on_update_downloaded(self, zip_path, info: dict) -> None:
        """下载完成（主线程）：确认后安装；用户选「以后再说」则下次启动再问。"""
        if zip_path is None:
            self._toast("⚠️ 更新包下载失败，下次启动再试", error=True)
            return
        body = (info.get("body") or "").strip()
        if len(body) > 400:
            body = body[:400] + "…"
        confirmed = messagebox.askyesno(
            "发现新版本",
            f"ClipVault v{info['version']} 已下载完成。\n\n{body}\n\n"
            "是否立即重启并更新？（更新只替换程序文件，不动你的数据）",
            parent=self.root,
        )
        if not confirmed:
            self._toast("已取消，更新包已缓存，下次启动再问")
            return
        if not updater.install_update(Path(zip_path)):
            messagebox.showwarning(
                "自动更新不可用",
                f"当前环境不支持自动替换，请手动下载：\n{info.get('html_url') or updater.API_LATEST}",
                parent=self.root,
            )
            return
        self._toast("🚀 正在安装更新，应用即将重启…")
        # 给 toast 一点渲染时间，再停采集、退窗口（安装脚本会等进程退出后动文件）
        stop_event = getattr(self, "stop_event", None)
        if stop_event is not None:
            stop_event.set()
        self.root.after(600, self.root.destroy)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """启动 GUI；默认同时拉起采集器线程（--no-watch 可关闭）。"""
    argv = argv if argv is not None else sys.argv[1:]
    if tk is None:
        print("当前环境没有 tkinter，无法启动图形界面。")
        raise SystemExit(1)

    storage.init_db()

    stop_event = threading.Event()
    pause_event = threading.Event()
    if "--no-watch" not in argv:
        try:
            import watcher
        except ImportError as exc:
            print(f"剪贴板采集不可用（缺少依赖）：{exc}\n仅启动界面。", flush=True)
        else:
            threading.Thread(
                target=watcher.run_collector,
                args=(stop_event, pause_event),
                name="clipvault-collector",
                daemon=True,
            ).start()
            print("ClipVault 已启动：采集器运行中，界面已打开。", flush=True)
    else:
        print("ClipVault 已启动（--no-watch：仅界面，不采集）。", flush=True)

    root = tk.Tk()
    gui = ClipVaultGUI(root)  # 实例由 root.after 回调与控件事件链路持有，无需外部引用
    gui.stop_event = stop_event  # 更新重启时要先停采集
    gui.start_update_check()  # 后台检查 GitHub Release（源码模式自动跳过）

    def on_close():
        stop_event.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
