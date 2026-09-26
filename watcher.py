"""watcher.py — 剪贴板监听主循环（Windows，常驻后台）。

职责：
  1. 每 0.5 秒轮询一次剪贴板；优先识别图片（CF_DIB），再识别文本（CF_UNICODETEXT），
     避免图片被当成文本误入库；
  2. 文本直接写 SQLite（storage.insert_item）；
     图片用 PIL.ImageGrab.grabclipboard() 拿到 PIL.Image；
  3. 原图保存到 clipboard_data/images/，文件名格式：日期_时间_随机6位.png；
     同时生成 200x200 缩略图，文件名加 thumb_ 前缀；数据库只存相对路径；
  4. 文本哈希 = sha256(text.encode())；
     图片哈希 = PIL.Image 转 RGB 存 PNG 后的字节哈希（像素级，与文件元数据无关）；
  5. 维护全局变量 last_hash，内容不变不重复写入（库内 content_hash UNIQUE 兜底）；
  6. OpenClipboard 被其他程序独占时，重试 3 次、每次间隔 50ms；
  7. 循环内任何单轮异常都被捕获并跳过，保证进程 24 小时不退出；
  8. 新文本入库后把「AI 分类 + 语义向量化」任务丢进后台队列（ai_client），
     未配置 AI 时自动跳过，不影响采集性能。

依赖：pywin32（win32clipboard/win32con/win32gui）、Pillow（ImageGrab/ImageOps）。
运行：python watcher.py
"""

from __future__ import annotations

import hashlib
import random
import struct
import threading
import time
from datetime import datetime
from io import BytesIO

import pywintypes
import win32clipboard
import win32con
import win32gui
from PIL import Image, ImageGrab, ImageOps

from ai_client import enqueue_analysis
from storage import (
    IMAGE_DIR,
    find_by_hash,
    init_db,
    insert_item,
    to_relative,
)

# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------

#: 轮询间隔（秒）
POLL_INTERVAL: float = 0.5

#: OpenClipboard 失败时的重试次数
OPEN_RETRIES: int = 3

#: OpenClipboard 重试间隔（秒）
OPEN_RETRY_DELAY: float = 0.05

#: 缩略图尺寸（宽, 高）
THUMBNAIL_SIZE: tuple[int, int] = (200, 200)

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

#: 最近一次成功处理的哈希；剪贴板内容没变就直接跳过，不重复写库
last_hash: str | None = None

#: 剪贴板繁忙告警的日志防抖：避免被长时间占用时刷屏
_clipboard_busy_logged: bool = False


# ---------------------------------------------------------------------------
# 剪贴板基础操作（带独占重试）
# ---------------------------------------------------------------------------


def _open_clipboard_with_retry() -> bool:
    """打开剪贴板；被其他程序独占导致失败时，按 50ms 间隔重试 3 次。

    返回 True 表示打开成功（调用方负责 CloseClipboard）；3 次都失败返回 False。
    """
    global _clipboard_busy_logged
    for attempt in range(1, OPEN_RETRIES + 1):
        try:
            win32clipboard.OpenClipboard()
            _clipboard_busy_logged = False
            return True
        except pywintypes.error:
            if not _clipboard_busy_logged:
                print("[警告] 剪贴板被其他程序占用，正在重试…", flush=True)
                _clipboard_busy_logged = True
            if attempt < OPEN_RETRIES:
                time.sleep(OPEN_RETRY_DELAY)
    return False


def _close_clipboard_safely() -> None:
    """关闭剪贴板；忽略 1418（线程没有打开剪贴板）等偶发错误。

    Win32 剪贴板在极少数竞态下 CloseClipboard 会报 1418（此时实际已经关了），
    继续走流程即可，不该把异常抛给上层。
    """
    try:
        win32clipboard.CloseClipboard()
    except pywintypes.error:
        pass


def probe_formats() -> tuple[bool, bool]:
    """探测剪贴板里有哪些格式，返回 (有图片, 有文本)。

    按需求约定：图片格式 CF_DIB 优先检查，文本 CF_UNICODETEXT 其次，
    避免图片同时以多种格式存在时被误判成文本。
    """
    if not _open_clipboard_with_retry():
        return False, False
    try:
        has_image = bool(win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB))
        has_text = bool(win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT))
    finally:
        _close_clipboard_safely()
    return has_image, has_text


def read_text() -> str | None:
    """从剪贴板读取 Unicode 文本；失败返回 None。"""
    if not _open_clipboard_with_retry():
        return None
    try:
        return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    finally:
        _close_clipboard_safely()


def _dib_to_image(dib: bytes) -> Image.Image:
    """把 DIB 字节流转成 PIL.Image。

    DIB（设备无关位图）比 BMP 文件少了 14 字节文件头，
    手工补一个标准 BITMAPFILEHEADER 后即可用 Pillow 解码。
    """
    header = (
        b"BM"
        + struct.pack("<I", 14 + len(dib))  # 文件总大小
        + struct.pack("<HH", 0, 0)  # 保留字段
        + struct.pack("<I", 14)  # 像素数据起始偏移
    )
    return Image.open(BytesIO(header + dib))


def grab_image() -> Image.Image | None:
    """从剪贴板获取图片，返回 PIL.Image 或 None。

    主路径：PIL.ImageGrab.grabclipboard()（新版 Pillow 已支持 Windows，返回 Image）。
    兜底路径（旧版 Pillow 的 grabclipboard 在 Windows 上返回 None）：
    手动读取 CF_DIB 数据自行解码，保证老版本 Pillow 也能正常工作。
    """
    image = ImageGrab.grabclipboard()
    if isinstance(image, Image.Image):
        return image

    # —— 旧版 Pillow 兜底：win32clipboard 直接取 CF_DIB 字节 ——
    if not _open_clipboard_with_retry():
        return None
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
            return None
        dib = win32clipboard.GetClipboardData(win32con.CF_DIB)
    finally:
        _close_clipboard_safely()
    if not isinstance(dib, (bytes, bytearray)):
        return None
    return _dib_to_image(bytes(dib))


def get_source_app() -> str:
    """获取当前前台窗口标题，作为"来源应用"信息；拿不到返回 'unknown'。"""
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        return title.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# 哈希与文件名
# ---------------------------------------------------------------------------


def image_hash(image: Image.Image) -> str:
    """图片内容哈希：统一转 RGB 后存成 PNG，对字节做 SHA-256。

    同样的像素内容哈希一定一致（不受压缩参数、元数据、像素排列的影响）。
    """
    normalized = image.convert("RGB")
    buffer = BytesIO()
    normalized.save(buffer, format="PNG")
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def text_hash(text: str) -> str:
    """文本内容哈希：sha256(text.encode())。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique_stem() -> str:
    """生成文件主干名：日期_时间_随机6位（如 20250601_143022_481920）。

    极小概率撞名时循环换随机数，确保不会覆盖已有文件。
    """
    now = datetime.now()
    while True:
        stem = f"{now:%Y%m%d_%H%M%S}_{random.randint(0, 999999):06d}"
        if not (IMAGE_DIR / f"{stem}.png").exists():
            return stem


# ---------------------------------------------------------------------------
# 入库处理
# ---------------------------------------------------------------------------


def process_image(image: Image.Image, source_app: str) -> None:
    """处理剪贴板图片：像素哈希去重 → 存原图 → 生成 200x200 缩略图 → 写库。"""
    global last_hash

    digest = image_hash(image)
    # 内容与上一条相同，或库里已有同哈希记录：跳过写入
    if digest == last_hash or find_by_hash(digest) is not None:
        last_hash = digest
        print("[跳过] 图片与已有内容重复", flush=True)
        return

    # 统一转成 RGB 再落盘，保证文件字节与上面参与哈希的字节完全一致
    normalized = image.convert("RGB")
    stem = _unique_stem()
    image_path = IMAGE_DIR / f"{stem}.png"
    thumbnail_path = IMAGE_DIR / f"thumb_{stem}.png"

    # 原图（RGB PNG）
    normalized.save(image_path, format="PNG")
    # 缩略图：居中裁剪成 200x200
    thumbnail = ImageOps.fit(normalized, THUMBNAIL_SIZE, Image.LANCZOS)
    thumbnail.save(thumbnail_path, format="PNG")

    item_id = insert_item(
        content_type="image",
        content_hash=digest,
        image_path=to_relative(image_path),
        thumbnail_path=to_relative(thumbnail_path),
        source_app=source_app,
    )
    if item_id is None:
        # 极端竞态下入库失败（UNIQUE 冲突）：删掉刚落盘的文件，避免孤儿图片
        image_path.unlink(missing_ok=True)
        thumbnail_path.unlink(missing_ok=True)
        print("[跳过] 图片入库失败，已清理临时文件", flush=True)
        return
    last_hash = digest
    print(
        f"[图片] #{item_id} {image_path.name} "
        f"（原图 {normalized.width}x{normalized.height}，来源：{source_app}）",
        flush=True,
    )


def process_text(text: str, source_app: str) -> None:
    """处理剪贴板文本：sha256 去重 → 直接写库。

    哈希口径与 storage.update_item_content 保持一致：先 strip 再哈希/入库。
    复制内容常带尾部换行，不统一口径会导致编辑后再复制同内容插入重复行。
    """
    global last_hash

    text = text.strip()
    if not text:
        return

    digest = text_hash(text)
    # 内容与上一条相同，或库里已有同哈希记录：跳过写入
    if digest == last_hash or find_by_hash(digest) is not None:
        last_hash = digest
        print("[跳过] 文本与已有内容重复", flush=True)
        return

    item_id = insert_item(
        content_type="text",
        content_hash=digest,
        text_content=text,
        source_app=source_app,
    )
    if item_id is not None:
        last_hash = digest
        preview = text if len(text) <= 50 else text[:50] + "…"
        print(f"[文本] #{item_id} {preview!r}（来源：{source_app}）", flush=True)
        # 新文本入库后，把「AI 分类 + 语义向量化」任务丢进后台队列（未配置 AI 时自动跳过）
        enqueue_analysis(item_id, text)


# ---------------------------------------------------------------------------
# 单轮采集与主循环
# ---------------------------------------------------------------------------


def capture_once() -> None:
    """单轮采集：先探测图片格式，再探测文本；图片优先处理。"""
    source_app = get_source_app()
    has_image, has_text = probe_formats()

    # —— 优先走图片分支，避免图片被 CF_UNICODETEXT 误判成文本 ——
    if has_image:
        image = grab_image()
        if image is not None:
            process_image(image, source_app)
            return
        print("[提示] 剪贴板报告有图片但未能解码，本轮忽略", flush=True)

    # —— 图片分支没有收获时再处理文本 ——
    if has_text:
        text = read_text()
        if text:
            process_text(text, source_app)


#: 错误日志防抖间隔：同一条错误该间隔内只打印一次（秒）
LOG_THROTTLE_SECONDS = 10.0

#: 上一条已打印的错误（用于防抖）：(错误文案, 单调时钟时间)
_last_error_log: tuple[str, float] | None = None


def _log_error_throttled(message: str) -> None:
    """打印循环内错误日志，同一条 10 秒内只出现一次。

    24 小时常驻场景下，数据目录被删 / 数据库不可用这类持续故障
    会每 0.5 秒触发一次，不防抖会把日志刷爆。
    """
    global _last_error_log
    now = time.monotonic()
    if (
        _last_error_log is not None
        and _last_error_log[0] == message
        and now - _last_error_log[1] < LOG_THROTTLE_SECONDS
    ):
        return
    _last_error_log = (message, now)
    print(message, flush=True)


def run_collector(stop_event: threading.Event, pause_event: threading.Event) -> None:
    """采集循环主体（命令行与托盘共用）。

    - stop_event：置位后退出循环；
    - pause_event：置位后暂停采集（托盘「暂停监听」用），未置位时正常运行。
    """
    init_db()
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        if not pause_event.is_set():
            try:
                capture_once()
            except pywintypes.error as exc:
                _log_error_throttled(f"[警告] 剪贴板访问异常（跳过本轮）：{exc}")
            except Exception as exc:  # 单轮异常绝不让循环退出
                _log_error_throttled(f"[错误] 处理剪贴板内容失败（跳过本轮）：{exc}")
        # 用 wait 代替 sleep：停止/暂停信号能立即响应，不必等满一个轮询周期
        stop_event.wait(POLL_INTERVAL)


def main() -> None:
    """命令行入口：初始化数据库后进入无限轮询循环，Ctrl+C 退出。"""
    stop_event = threading.Event()
    pause_event = threading.Event()

    print("=" * 64, flush=True)
    print("ClipVault 剪贴板监听已启动", flush=True)
    print(f"轮询间隔 {POLL_INTERVAL}s | 重试 {OPEN_RETRIES} 次 x {OPEN_RETRY_DELAY}s", flush=True)
    print(f"图片目录 {IMAGE_DIR} | 数据库见 storage.DB_PATH", flush=True)
    print("按 Ctrl+C 停止", flush=True)
    print("=" * 64, flush=True)

    try:
        run_collector(stop_event, pause_event)
    except KeyboardInterrupt:
        print("\n已停止监听。", flush=True)


if __name__ == "__main__":
    main()
