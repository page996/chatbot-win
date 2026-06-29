from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.control.sidebar_api import build_sidebar_state, sidebar_queue_action
from app.personal_wechat_bot.wechat_driver.windows_readonly import Win32WindowProbe, foreground_window_info


def run_sidebar_window(data_dir: str | Path = "data", *, poll_interval_ms: int = 2000) -> None:
    app = SidebarWindow(Path(data_dir), poll_interval_ms=poll_interval_ms)
    app.run()


class SidebarWindow:
    def __init__(self, data_dir: Path, *, poll_interval_ms: int = 2000):
        self.data_dir = data_dir
        self.poll_interval_ms = poll_interval_ms
        self.root = tk.Tk()
        self.root.title("WeChat Agent Queue")
        self.root.geometry("360x680")
        self.root.attributes("-topmost", True)
        self.status_var = tk.StringVar(value="Loading...")
        self.mode_var = tk.StringVar(value="")
        self.queue_frame: ttk.Frame | None = None
        self._build()

    def run(self) -> None:
        self.refresh()
        self.root.mainloop()

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        header = ttk.Frame(self.root, padding=8)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Send Audit", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, wraplength=320).grid(row=1, column=0, sticky="w")
        ttk.Button(header, text="Refresh", command=self.refresh).grid(row=0, column=1, rowspan=2, padx=(8, 0))

        ttk.Label(self.root, textvariable=self.mode_var, padding=(8, 0)).grid(row=1, column=0, sticky="w")
        self.queue_frame = ttk.Frame(self.root, padding=8)
        self.queue_frame.grid(row=2, column=0, sticky="nsew")
        self.root.rowconfigure(2, weight=1)

    def refresh(self) -> None:
        try:
            state = build_sidebar_state(self.data_dir)
            self._render(state)
            self._dock_near_wechat()
        except Exception as exc:
            self.status_var.set(f"Load failed: {type(exc).__name__}: {exc}")
        finally:
            self.root.after(self.poll_interval_ms, self.refresh)

    def _render(self, state: dict[str, Any]) -> None:
        readiness = state.get("readiness", {})
        summary = readiness.get("summary", {}) if isinstance(readiness, dict) else {}
        self.status_var.set(
            f"{readiness.get('status', 'unknown')} | blockers {summary.get('blockers', 0)} | warnings {summary.get('warnings', 0)}"
        )
        config = state.get("config", {})
        self.mode_var.set(
            f"mode={config.get('mode', '')} send_enabled={config.get('send_enabled', '')} driver={config.get('send_driver', '')}"
        )
        assert self.queue_frame is not None
        for child in self.queue_frame.winfo_children():
            child.destroy()
        queues = state.get("queues", {})
        items = []
        for status in ["pending", "approved", "failed"]:
            queue = queues.get(status, {}) if isinstance(queues, dict) else {}
            for item in queue.get("items", []) if isinstance(queue, dict) else []:
                if isinstance(item, dict):
                    items.append(item)
        if not items:
            ttk.Label(self.queue_frame, text="No queue items").grid(row=0, column=0, sticky="w")
            return
        for row, item in enumerate(items):
            self._queue_item(self.queue_frame, row, item)

    def _queue_item(self, parent: ttk.Frame, row: int, item: dict[str, Any]) -> None:
        card = ttk.Frame(parent, padding=8, relief="ridge")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        card.columnconfigure(0, weight=1)
        reply = item.get("reply", {}) if isinstance(item.get("reply"), dict) else {}
        ttk.Label(card, text=f"{item.get('status', '')} | {reply.get('conversation_id', '')}").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text=str(reply.get("text", "")), wraplength=310).grid(row=1, column=0, sticky="ew", pady=(4, 4))
        buttons = ttk.Frame(card)
        buttons.grid(row=2, column=0, sticky="w")
        queue_id = str(item.get("queue_id", ""))
        status = str(item.get("status", ""))
        if status == "pending":
            ttk.Button(buttons, text="Approve", command=lambda: self._queue_action(queue_id, "approve")).grid(row=0, column=0)
            ttk.Button(buttons, text="Reject", command=lambda: self._queue_action(queue_id, "reject")).grid(row=0, column=1, padx=4)
        if status == "approved":
            ttk.Button(buttons, text="Send 3s", command=lambda: self._send_after_delay(queue_id)).grid(row=0, column=0)
            ttk.Button(buttons, text="Reject", command=lambda: self._queue_action(queue_id, "reject")).grid(row=0, column=1, padx=4)

    def _queue_action(self, queue_id: str, action: str) -> None:
        try:
            sidebar_queue_action(self.data_dir, action, queue_id, {"reviewer": "sidebar_window"})
            self.status_var.set(f"{action} ok")
            self.refresh()
        except Exception as exc:
            self.status_var.set(f"{action} failed: {type(exc).__name__}: {exc}")

    def _send_after_delay(self, queue_id: str) -> None:
        def worker() -> None:
            for remaining in [3, 2, 1]:
                self.root.after(0, lambda value=remaining: self.status_var.set(f"Switch to target WeChat chat: {value}s"))
                time.sleep(1)
            self.root.after(0, lambda: self._queue_action(queue_id, "send-approved"))

        threading.Thread(target=worker, daemon=True).start()

    def _dock_near_wechat(self) -> None:
        foreground = foreground_window_info()
        if _looks_like_wechat_foreground(foreground):
            left = int(foreground.get("right", 0) or 0) + 8
            top = int(foreground.get("top", 80) or 80)
            self.root.geometry(f"360x680+{max(left, 0)}+{max(top, 0)}")
            return
        windows = Win32WindowProbe(include_invisible=False).find_wechat_windows()
        if not windows:
            return
        window = windows[0]
        self.root.geometry(f"360x680+{max(window.right + 8, 0)}+{max(window.top, 0)}")


def _looks_like_wechat_foreground(foreground: dict[str, Any]) -> bool:
    process = str(foreground.get("process_name", "")).lower()
    title = str(foreground.get("title", "")).lower()
    return process in {"wechat.exe", "weixin.exe", "wechatappex.exe"} or "wechat" in title or "微信" in title
