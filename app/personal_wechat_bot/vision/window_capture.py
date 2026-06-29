from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass
from pathlib import Path

from ctypes import wintypes


@dataclass(frozen=True)
class WindowCaptureResult:
    path: str
    hwnd: int
    title: str
    width: int
    height: int
    ok: bool
    reason: str = ""


class Win32WindowCapture:
    def capture(self, hwnd: int, output_path: str | Path, *, mode: str = "window") -> WindowCaptureResult:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        if not user32.IsWindow(hwnd):
            return WindowCaptureResult(str(output_path), hwnd, "", 0, 0, False, "invalid_window")
        if mode not in {"window", "screen", "auto"}:
            raise ValueError("capture mode must be window, screen, or auto")

        title = _window_title(hwnd)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return WindowCaptureResult(str(output_path), hwnd, title, 0, 0, False, "get_window_rect_failed")

        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return WindowCaptureResult(str(output_path), hwnd, title, width, height, False, "empty_window_rect")

        if mode == "screen":
            return self._capture_screen_rect(hwnd, output_path, title, rect)

        hwnd_dc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old = gdi32.SelectObject(mem_dc, bmp)
        ok = user32.PrintWindow(hwnd, mem_dc, 2)
        if not ok:
            ok = gdi32.BitBlt(mem_dc, 0, 0, width, height, hwnd_dc, 0, 0, 0x00CC0020)

        image_bytes = _read_bitmap_bytes(gdi32, mem_dc, bmp, width, height)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        if image_bytes is None:
            return WindowCaptureResult(str(output_path), hwnd, title, width, height, False, "get_dibits_failed")
        if mode == "auto" and _looks_blank(image_bytes):
            return self._capture_screen_rect(hwnd, output_path, title, rect)
        _write_bmp(output_path, width, height, image_bytes)
        return WindowCaptureResult(str(output_path), hwnd, title, width, height, bool(ok), "")

    def _capture_screen_rect(
        self,
        hwnd: int,
        output_path: str | Path,
        title: str,
        rect: wintypes.RECT,
    ) -> WindowCaptureResult:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmp = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        old = gdi32.SelectObject(mem_dc, bmp)
        ok = gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, rect.left, rect.top, 0x00CC0020)
        image_bytes = _read_bitmap_bytes(gdi32, mem_dc, bmp, width, height)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
        if image_bytes is None:
            return WindowCaptureResult(str(output_path), hwnd, title, width, height, False, "get_dibits_failed")
        _write_bmp(output_path, width, height, image_bytes)
        return WindowCaptureResult(str(output_path), hwnd, title, width, height, bool(ok), "screen")


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


def _read_bitmap_bytes(gdi32: object, mem_dc: int, bmp: int, width: int, height: int) -> bytes | None:
    row_bytes = ((width * 24 + 31) // 32) * 4
    image_size = row_bytes * height
    buffer = ctypes.create_string_buffer(image_size)
    info = _BitmapInfo()
    info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 24
    info.bmiHeader.biCompression = 0
    info.bmiHeader.biSizeImage = image_size
    result = gdi32.GetDIBits(mem_dc, bmp, 0, height, buffer, ctypes.byref(info), 0)
    if not result:
        return None
    return buffer.raw


def _write_bmp(path: str | Path, width: int, height: int, image_bytes: bytes) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    offset = 54
    file_size = offset + len(image_bytes)
    with output.open("wb") as f:
        f.write(b"BM")
        f.write(struct.pack("<IHHI", file_size, 0, 0, offset))
        f.write(struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(image_bytes), 0, 0, 0, 0))
        f.write(image_bytes)


def _window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _looks_blank(image_bytes: bytes) -> bool:
    if not image_bytes:
        return True
    sample = image_bytes[:: max(1, len(image_bytes) // 4096)]
    return max(sample) - min(sample) <= 2
