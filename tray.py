"""tray.py — ClipVault 系统托盘常驻版（Windows）。

依赖：tkinter（Python 内置，GUI 主线程）、Pillow；pystray 为可选
（pip install -e ".[tray]"），没装则退化为纯 GUI 窗口。
采集需要 pywin32。运行：python tray.py   （或打包后的 ClipVault.exe）

进程结构：
  - 主线程：tkinter GUI（gui.ClipVaultGUI）；
  - 后台线程：剪贴板采集（watcher.run_collector）；
  - 后台线程（可选）：pystray 托盘图标。
关闭窗口 = 隐藏到托盘（托盘在时）；托盘菜单「退出」才真正结束。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import ai_client
import autostart
import storage
from gui import ClipVaultGUI

# pystray 是可选依赖；Pillow 是运行必需。分开捕获，给出准确安装提示
try:
    import pystray
except ImportError:
    pystray = None  # type: ignore[assignment]

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

logger = logging.getLogger("clipvault.tray")

#: 资源基准：源码模式是项目根目录，PyInstaller 打包后是 _MEIPASS 解包目录
_ASSET_BASE: Path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

#: 托盘图标文件（由 make_icon.py 生成，打包时随 assets/ 一起进去）
ICON_PATH: Path = _ASSET_BASE / "assets" / "tray.png"


class TrayApp:
    """托盘 + GUI 组装：持有采集线程与托盘图标，统一启停。"""

    def __init__(self, root) -> None:
        self.root = root
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.quit_requested = threading.Event()
        self.icon = None  # pystray.Icon
        self._ai_status_cache: tuple[str, float] | None = None
        self._gui = None  # ClipVaultGUI 实例（保引用）

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------

    def start_collector(self) -> None:
        """启动剪贴板采集线程（守护线程）。"""
        storage.init_db()
        import watcher

        threading.Thread(
            target=watcher.run_collector,
            args=(self.stop_event, self.pause_event),
            name="clipvault-collector",
            daemon=True,
        ).start()

    def start_tray(self) -> bool:
        """在守护线程里启动托盘图标；pystray 不可用时返回 False。"""
        if pystray is None:
            return False
        image = self._load_icon_image()
        self.icon = pystray.Icon(
            name="ClipVault",
            icon=image,
            title="ClipVault 剪贴板管理器",
            menu=self.build_menu(),
        )
        threading.Thread(target=self.icon.run, name="clipvault-tray", daemon=True).start()
        return True

    # ------------------------------------------------------------------
    # 托盘菜单动作（跨线程操作 GUI 一律走 root.after）
    # ------------------------------------------------------------------

    def open_window(self, icon=None, item=None) -> None:
        self.root.after(0, self.root.deiconify)
        self.root.after(0, self.root.lift)

    def open_settings(self, icon=None, item=None) -> None:
        """打开 GUI 的 AI 设置窗口（跨线程，走主线程 after）。"""
        if self._gui is None:
            return
        self.root.after(0, self._gui.open_settings)
        self.root.after(0, self.root.deiconify)

    def toggle_pause(self, icon=None, item=None) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            logger.info("采集已恢复")
        else:
            self.pause_event.set()
            logger.info("采集已暂停")
        self._refresh_tray_menu()

    def _refresh_tray_menu(self) -> None:
        """刷新托盘菜单。

        pystray 的 win32 后端只在启动时构建一次 HMENU，checked/enabled/text
        回调不会自动重生效；状态变化后必须显式 update_menu，否则勾选不动、
        「立即补建语义向量」配好 AI 也点不了。
        """
        if self.icon is None:
            return
        try:
            self.icon.update_menu()
        except Exception as exc:  # pragma: no cover - 刷新失败不影响主流程
            logger.debug("托盘菜单刷新失败：%s", exc)

    def is_paused(self, item) -> bool:
        return self.pause_event.is_set()

    def toggle_autostart(self, icon=None, item=None) -> None:
        try:
            if autostart.is_installed():
                autostart.remove()
                self.notify("已关闭开机自启动")
            else:
                autostart.install()
                self.notify("已开启开机自启动（写入 HKCU 运行项）")
        except Exception as exc:
            logger.warning("切换开机自启动失败：%s", exc)
            self.notify(f"操作失败：{exc}")
        self._refresh_tray_menu()

    def autostart_enabled(self, item) -> bool:
        return autostart.is_installed()

    def ai_enabled(self, item) -> bool:
        return ai_client.is_configured()

    def ai_status_text(self, item) -> str:
        """AI 状态菜单文案（统计开库，加 1.5s 缓存避免频繁 IO）。"""
        if not ai_client.is_configured():
            self._ai_status_cache = ("AI：未配置（详见 README）", time.monotonic())
            return self._ai_status_cache[0]
        now = time.monotonic()
        cached = self._ai_status_cache
        if cached and now - cached[1] < 1.5:
            return cached[0]
        stats = storage.stats()
        text = f"AI：已配置 · 待向量 {stats['pending_vectors']} 条"
        self._ai_status_cache = (text, now)
        return text

    def reindex_ai(self, icon=None, item=None) -> None:
        if not ai_client.is_configured():
            self.notify("AI 未配置：请设置 CLIPVAULT_AI_API_KEY 后重试")
            return
        rows = storage.items_without_vector(500)
        count = ai_client.enqueue_reindex([(r["id"], r["text_content"] or "") for r in rows])
        self.notify(f"已提交 {count} 条语义向量任务到后台队列")

    def request_quit(self, icon=None, item=None) -> None:
        """托盘「退出」：通知主线程销毁窗口。"""
        self.quit_requested.set()

    def notify(self, message: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(message, title="ClipVault")
        except Exception as exc:  # pragma: no cover - 通知失败无关紧要
            logger.debug("托盘通知失败：%s", exc)

    # ------------------------------------------------------------------
    # 菜单与启动
    # ------------------------------------------------------------------

    def build_menu(self):
        """构建托盘菜单（pystray 支持 callable 文本/勾选/置灰）。"""
        return pystray.Menu(
            pystray.MenuItem("打开界面", self.open_window, default=True),
            pystray.MenuItem("AI 设置…", self.open_settings),
            pystray.MenuItem("暂停采集", self.toggle_pause, checked=self.is_paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启动", self.toggle_autostart, checked=self.autostart_enabled),
            pystray.MenuItem(self.ai_status_text, lambda icon, item: None),
            pystray.MenuItem("立即补建语义向量", self.reindex_ai, enabled=self.ai_enabled),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.request_quit),
        )

    def run(self) -> None:
        """组装并进入 tkinter 主循环（阻塞）。"""
        self.start_collector()
        # 先建 GUI（托盘菜单要操作它），再起托盘线程
        self._gui = ClipVaultGUI(self.root)
        # GUI 设置变更后刷新托盘动态菜单（AI 状态/补建向量置灰）
        self._gui.notify_hook = self._refresh_tray_menu
        tray_ok = self.start_tray()
        if not tray_ok:
            print('未安装 pystray，本次以纯窗口模式运行（pip install -e ".[tray]" 可启用托盘）。')

        def on_close():
            if tray_ok:
                self.root.withdraw()  # 隐藏到托盘，继续采集
            else:
                self.request_quit()

        self.root.protocol("WM_DELETE_WINDOW", on_close)

        # 轮询退出请求（tkinter 非线程安全，跨线程销毁窗口走主线程轮询）
        def poll_quit():
            if self.quit_requested.is_set():
                self.stop_event.set()
                self.root.destroy()
                return
            self.root.after(200, poll_quit)

        self.root.after(200, poll_quit)

        if tray_ok and self.icon is not None:
            self.icon.notify("ClipVault 已启动\n采集 + 界面运行中", title="ClipVault")
        self.root.mainloop()

        # 主循环结束：兜底停止
        self.stop_event.set()
        if self.icon is not None:
            self.icon.stop()

    def _load_icon_image(self):
        """加载托盘图标；文件缺失/损坏时现画一个 64x64 的兜底图标。"""
        if ICON_PATH.is_file():
            try:
                with Image.open(ICON_PATH) as im:
                    return im.copy()  # copy 后关闭文件句柄
            except Exception as exc:
                logger.warning("托盘图标加载失败，使用兜底图标：%s", exc)
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(59, 125, 221, 255))
        return image


def main() -> None:
    """入口：检查依赖 -> 启动托盘 + GUI。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        import tkinter as tk
    except ImportError:
        print("当前环境没有 tkinter，无法启动图形界面。")
        raise SystemExit(1) from None
    if Image is None:
        print("缺少依赖 Pillow。请先安装：pip install -r requirements.txt")
        raise SystemExit(1)
    storage.init_db()
    root = tk.Tk()
    TrayApp(root).run()


if __name__ == "__main__":
    main()
