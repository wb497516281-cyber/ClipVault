"""clipwriter.py — 剪贴板写回：把文本/图片写入系统剪贴板。

供原生 GUI（gui.py）与托盘（tray.py）共用。
依赖：pywin32（win32clipboard/win32con）、Pillow；标准库 ctypes 用于 CF_BITMAP。

图片采用多格式写入，保证 QQ / 微信 / 企业微信都能粘贴：
  - CF_DIB              标准设备无关位图（QQ 及大多数软件）；
  - CF_BITMAP           HBITMAP 句柄（部分老软件、资源管理器）；
  - 注册格式 "PNG"       微信 / 企业微信优先解析 PNG；
  - 注册格式 "image/png" 兼容另一种命名的写法。
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path

import pywintypes
import win32clipboard
import win32con
from PIL import Image

# ---------------------------------------------------------------------------
# 基础操作（带独占重试，与 watcher 保持一致的 3 次 x 50ms 策略）
# ---------------------------------------------------------------------------


def _open_clipboard_with_retry() -> bool:
    """打开剪贴板；被其他程序独占时按 50ms 间隔重试 3 次。"""
    for attempt in range(1, 4):
        try:
            win32clipboard.OpenClipboard()
            return True
        except pywintypes.error:
            if attempt < 3:
                time.sleep(0.05)
    return False


def _close_clipboard_safely() -> None:
    """关闭剪贴板；忽略 1418（线程没有打开剪贴板）等偶发错误。"""
    try:
        win32clipboard.CloseClipboard()
    except pywintypes.error:
        pass


class ClipboardBusyError(RuntimeError):
    """剪贴板被其他程序持续占用，重试后仍打不开。"""


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------


def set_clipboard_text(text: str) -> None:
    """把文本写回系统剪贴板（CF_UNICODETEXT，QQ/微信输入框均读取）。"""
    if not _open_clipboard_with_retry():
        raise ClipboardBusyError("剪贴板被其他程序占用，请稍后重试")
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        _close_clipboard_safely()


# ---------------------------------------------------------------------------
# 图片（纯函数部分，便于测试）
# ---------------------------------------------------------------------------


def image_to_dib(image_path: Path) -> bytes:
    """把图片文件转换成可直接写入剪贴板的 DIB 字节（纯函数）。

    Windows 剪贴板图片用 DIB 格式：先让 Pillow 把图片转成 24 位 BMP，
    再去掉 BMP 文件头（前 14 字节 BITMAPFILEHEADER），剩下的就是 DIB 数据
    （以 40 字节 BITMAPINFOHEADER 开头，前 4 字节即头大小 40）。
    """
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")  # 24 位 BMP，兼容性最好
        buffer = BytesIO()
        rgb.save(buffer, format="BMP")
        return buffer.getvalue()[14:]  # 去掉文件头 -> DIB


def image_to_png(image_path: Path) -> bytes:
    """把图片文件转成 PNG 字节（纯函数）。

    微信 / 企业微信粘贴图片时优先解析剪贴板里的 PNG 注册格式。
    """
    with Image.open(image_path) as img:
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()


def _dib_to_hbitmap(dib: bytes) -> int | None:
    """用 CreateDIBSection 从 DIB 数据创建 HBITMAP，返回句柄；失败返回 None。

    CF_BITMAP 是部分老软件/资源管理器粘贴图片时读取的格式。
    这里用 ctypes 调 gdi32，不引入第三方依赖；任何异常都安全降级
    （调用方仍有 CF_DIB + PNG 兜底）。
    注意：返回的句柄放入剪贴板后由系统接管，调用方不得删除。
    """
    try:
        import ctypes
        from ctypes import wintypes

        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            wintypes.LPVOID,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        header_size = int.from_bytes(dib[:4], "little")  # BITMAPINFOHEADER 大小（40）
        if header_size < 40 or header_size >= len(dib):
            return None
        screen_dc = ctypes.windll.user32.GetDC(None)  # type: ignore[attr-defined]
        try:
            bits = ctypes.c_void_p(0)
            info = ctypes.create_string_buffer(dib, len(dib))
            handle = gdi32.CreateDIBSection(screen_dc, info, 0, ctypes.byref(bits), None, 0)
            if not handle:
                return None
            # CreateDIBSection 不初始化像素，把 DIB 的像素数据拷进去
            pixel_bytes = dib[header_size:]
            if pixel_bytes:
                ctypes.memmove(bits, pixel_bytes, len(pixel_bytes))
            return int(handle)
        finally:
            ctypes.windll.user32.ReleaseDC(None, screen_dc)  # type: ignore[attr-defined]
    except Exception:
        return None


def set_clipboard_image(image_path: Path) -> None:
    """把图片写回系统剪贴板（多格式，保证 QQ / 微信 / 企业微信等都能粘贴）。

    DIB 放在最前：经典软件按枚举顺序取第一个支持的格式；
    按名查找 PNG 的软件（微信）不受顺序影响。
    """
    dib = image_to_dib(image_path)
    png = image_to_png(image_path)
    hbitmap = _dib_to_hbitmap(dib)

    if not _open_clipboard_with_retry():
        raise ClipboardBusyError("剪贴板被其他程序占用，请稍后重试")
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
        if hbitmap:
            try:
                win32clipboard.SetClipboardData(win32con.CF_BITMAP, hbitmap)
            except Exception:  # 写 BITMAP 失败不影响其余格式
                pass
        png_format = win32clipboard.RegisterClipboardFormat("PNG")
        win32clipboard.SetClipboardData(png_format, png)
        try:
            alt_png_format = win32clipboard.RegisterClipboardFormat("image/png")
            win32clipboard.SetClipboardData(alt_png_format, png)
        except Exception:  # 该名称注册失败就跳过
            pass
    finally:
        _close_clipboard_safely()
