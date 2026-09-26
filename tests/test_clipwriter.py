"""tests/test_clipwriter.py — 剪贴板写回测试：格式转换与 QQ/微信粘贴兼容性。

纯函数部分任何平台可跑；真实剪贴板用例在剪贴板不可用时自动跳过。
"""

from __future__ import annotations

import io
import struct

import pytest
import win32clipboard
import win32con
from PIL import Image

import clipwriter
import storage


@pytest.fixture(autouse=True)
def clean_db():
    """每个用例前清空业务表（clipwriter 的图片文件用例会写库）。"""
    storage.init_db()
    yield


# ---------------------------------------------------------------------------
# 纯函数：格式转换
# ---------------------------------------------------------------------------


def test_image_to_dib_strips_bmp_header(tmp_path):
    """DIB 转换：BMP 去文件头后应以 40 字节 BITMAPINFOHEADER 开头。"""
    image_file = tmp_path / "probe.png"
    Image.new("RGB", (32, 24), (1, 2, 3)).save(image_file, format="PNG")

    dib = clipwriter.image_to_dib(image_file)
    assert not dib.startswith(b"BM")  # 文件头已去掉
    assert struct.unpack("<I", dib[:4])[0] == 40  # BITMAPINFOHEADER 大小
    width, height = struct.unpack("<ii", dib[4:12])
    assert (width, height) == (32, 24)


def test_image_to_png_roundtrip(tmp_path):
    """PNG 转换：魔数正确，且能无损读回原尺寸与像素。"""
    image_file = tmp_path / "probe2.png"
    Image.new("RGB", (48, 36), (200, 100, 50)).save(image_file, format="PNG")

    png = clipwriter.image_to_png(image_file)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG 魔数
    with Image.open(io.BytesIO(png)) as img:
        assert img.size == (48, 36)
        assert img.getpixel((0, 0)) == (200, 100, 50)


def test_dib_to_hbitmap_returns_usable_handle(tmp_path):
    """HBITMAP 构造：Windows 上应返回非零句柄（GDI 不可用时允许 None 降级）。"""
    image_file = tmp_path / "probe3.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(image_file, format="PNG")
    dib = clipwriter.image_to_dib(image_file)

    handle = clipwriter._dib_to_hbitmap(dib)
    if handle is None:
        return  # GDI 不可用的环境允许降级
    assert isinstance(handle, int) and handle > 0
    try:
        import ctypes

        ctypes.windll.gdi32.DeleteObject(handle)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 真实剪贴板：QQ / 微信 / 企业微信 粘贴兼容性
# ---------------------------------------------------------------------------


def _clipboard_available() -> bool:
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def _clear_clipboard() -> None:
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.CloseClipboard()
    except Exception:
        pass


def test_set_clipboard_image_writes_im_friendly_formats(tmp_path):
    """复制图片回剪贴板后，QQ/微信需要的格式必须都在。

    多格式策略：
      - CF_DIB（QQ 及大多数软件）
      - CF_BITMAP（部分老软件）
      - 注册格式 PNG（微信/企业微信优先解析）
    剪贴板不可用的环境自动跳过。
    """
    if not _clipboard_available():
        pytest.skip("当前环境剪贴板不可用")

    image_file = tmp_path / "im_probe.png"
    Image.new("RGB", (64, 48), (12, 34, 56)).save(image_file, format="PNG")
    try:
        clipwriter.set_clipboard_image(image_file)

        win32clipboard.OpenClipboard()
        try:
            png_format = win32clipboard.RegisterClipboardFormat("PNG")
            has_dib = bool(win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB))
            has_png = bool(win32clipboard.IsClipboardFormatAvailable(png_format))
            png_bytes = win32clipboard.GetClipboardData(png_format) if has_png else b""
        finally:
            win32clipboard.CloseClipboard()

        assert has_dib, "缺少 CF_DIB（QQ 等软件需要）"
        assert has_png, "缺少 PNG 注册格式（微信/企业微信需要）"
        if png_bytes:
            with Image.open(io.BytesIO(png_bytes)) as img:
                assert img.size == (64, 48)
        # CF_BITMAP 为尽力而为格式（GDI 失败时降级），DIB + PNG 才是硬要求
    finally:
        _clear_clipboard()


def test_set_clipboard_text_writes_unicode_text():
    """复制文本回剪贴板后，CF_UNICODETEXT 必须存在（QQ/微信输入框读取）。"""
    if not _clipboard_available():
        pytest.skip("当前环境剪贴板不可用")

    clipwriter.set_clipboard_text("QQ微信粘贴测试 123")
    win32clipboard.OpenClipboard()
    try:
        has_text = bool(win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT))
        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) if has_text else ""
    finally:
        win32clipboard.CloseClipboard()
    assert has_text
    assert data == "QQ微信粘贴测试 123"
    _clear_clipboard()
