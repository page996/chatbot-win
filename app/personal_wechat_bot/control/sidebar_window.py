from __future__ import annotations

import hashlib
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

from app.personal_wechat_bot.control.sidebar_api import (
    build_sidebar_state,
    sidebar_queue_action,
    update_sidebar_controls,
)
from app.personal_wechat_bot.wechat_driver.windows_readonly import Win32WindowProbe, foreground_window_info


WINDOW_WIDTH = 400
WINDOW_HEIGHT = 720
QUEUE_STATUSES = ("pending", "approved", "failed")
ALL_COUNT_STATUSES = ("pending", "approved", "failed", "rejected", "sent")

COLORS = {
    "bg": "#f5f7fb",
    "surface": "#ffffff",
    "surface_alt": "#eef2f7",
    "ink": "#111827",
    "muted": "#667085",
    "line": "#dde3ec",
    "accent": "#0f766e",
    "accent_soft": "#d9f4ef",
    "danger": "#b42318",
    "danger_soft": "#fee4e2",
    "warning": "#b54708",
    "warning_soft": "#fef0c7",
    "ready": "#067647",
    "ready_soft": "#dcfae6",
    "dark": "#17202e",
    "dark_muted": "#c7d1df",
}

STATUS_COLORS = {
    "pending": ("#b54708", "#fef0c7"),
    "approved": ("#067647", "#dcfae6"),
    "failed": ("#b42318", "#fee4e2"),
    "rejected": ("#667085", "#eef2f7"),
    "sent": ("#175cd3", "#d1e9ff"),
}


def run_sidebar_window(data_dir: str | Path = "data", *, poll_interval_ms: int = 2000) -> None:
    app = SidebarWindow(Path(data_dir), poll_interval_ms=poll_interval_ms)
    app.run()


def flatten_queue_items(state: dict[str, Any], statuses: tuple[str, ...] = QUEUE_STATUSES) -> list[dict[str, Any]]:
    queues = state.get("queues", {})
    if not isinstance(queues, dict):
        return []
    items: list[dict[str, Any]] = []
    for status in statuses:
        queue = queues.get(status, {})
        raw_items = queue.get("items", []) if isinstance(queue, dict) else []
        for item in raw_items if isinstance(raw_items, list) else []:
            if isinstance(item, dict):
                copied = dict(item)
                copied.setdefault("status", status)
                items.append(copied)
    return items


def queue_counts(state: dict[str, Any], statuses: tuple[str, ...] = ALL_COUNT_STATUSES) -> dict[str, int]:
    queues = state.get("queues", {})
    counts: dict[str, int] = {}
    for status in statuses:
        queue = queues.get(status, {}) if isinstance(queues, dict) else {}
        if isinstance(queue, dict) and isinstance(queue.get("count"), int):
            counts[status] = int(queue["count"])
        else:
            counts[status] = len(queue.get("items", [])) if isinstance(queue, dict) else 0
    return counts


def sidebar_state_fingerprint(state: dict[str, Any]) -> str:
    config = state.get("config", {}) if isinstance(state.get("config"), dict) else {}
    readiness = state.get("readiness", {}) if isinstance(state.get("readiness"), dict) else {}
    probe = state.get("driver_probe", {}) if isinstance(state.get("driver_probe"), dict) else {}
    foreground = probe.get("foreground", {}) if isinstance(probe.get("foreground"), dict) else {}
    counts = queue_counts(state)
    item_parts: list[str] = []
    for item in flatten_queue_items(state, statuses=ALL_COUNT_STATUSES):
        reply = item.get("reply", {}) if isinstance(item.get("reply"), dict) else {}
        item_parts.append(
            "|".join(
                [
                    str(item.get("queue_id", "")),
                    str(item.get("status", "")),
                    str(reply.get("conversation_id", "")),
                    _digest(str(reply.get("text", ""))),
                    str(item.get("updated_at", "")),
                ]
            )
        )
    payload = "\n".join(
        [
            str(readiness.get("status", "")),
            str(config.get("mode", "")),
            str(config.get("send_enabled", "")),
            str(config.get("send_driver", "")),
            str(foreground.get("title", "")),
            str(sorted(counts.items())),
            *item_parts,
        ]
    )
    return _digest(payload)


class SidebarWindow:
    def __init__(self, data_dir: Path, *, poll_interval_ms: int = 2000):
        self.data_dir = data_dir
        self.poll_interval_ms = max(250, poll_interval_ms)
        self.root = tk.Tk()
        self.root.title("WeChat Agent Queue")
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.minsize(360, 560)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COLORS["bg"])

        self.status_var = tk.StringVar(value="Loading")
        self.status_detail_var = tk.StringVar(value="Preparing queue state")
        self.counts_var = tk.StringVar(value="Pending 0  Approved 0  Failed 0")
        self.driver_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="dry_run")
        self.send_enabled_var = tk.BooleanVar(value=False)
        self.dock_enabled_var = tk.BooleanVar(value=True)
        self.foreground_var = tk.StringVar(value="WeChat: searching")

        self.canvas: tk.Canvas | None = None
        self.items_frame: tk.Frame | None = None
        self.canvas_window: int | None = None
        self.footer_var = tk.StringVar(value="")

        self._refresh_after_id: str | None = None
        self._refreshing = False
        self._syncing_controls = False
        self._last_fingerprint = ""
        self._last_geometry = ""

        self._configure_style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def run(self) -> None:
        self.refresh(force=True)
        self.root.mainloop()

    def refresh(self, *, force: bool = False) -> None:
        if self._refreshing:
            self._schedule_refresh()
            return
        self._refreshing = True
        try:
            state = build_sidebar_state(self.data_dir)
            self._render(state, force=force)
            self._dock_near_wechat()
        except Exception as exc:
            self.status_var.set("Load failed")
            self.status_detail_var.set(f"{type(exc).__name__}: {exc}")
        finally:
            self._refreshing = False
            self._schedule_refresh()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Vertical.TScrollbar", gripcount=0, width=10, background=COLORS["surface_alt"])
        style.configure("TCombobox", fieldbackground=COLORS["surface"], background=COLORS["surface"])

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = tk.Frame(self.root, bg=COLORS["dark"], padx=14, pady=12)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="WeChat Agent",
            bg=COLORS["dark"],
            fg="white",
            font=("Segoe UI", 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._pill(header, self.status_var, fg=COLORS["ready"], bg=COLORS["ready_soft"]).grid(row=0, column=1, sticky="e")
        tk.Label(
            header,
            textvariable=self.status_detail_var,
            bg=COLORS["dark"],
            fg=COLORS["dark_muted"],
            font=("Segoe UI", 9),
            wraplength=340,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        controls = tk.Frame(self.root, bg=COLORS["bg"], padx=12, pady=10)
        controls.grid(row=1, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        self._build_controls(controls)

        queue_shell = tk.Frame(self.root, bg=COLORS["bg"], padx=10)
        queue_shell.grid(row=2, column=0, sticky="nsew")
        queue_shell.columnconfigure(0, weight=1)
        queue_shell.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(queue_shell, bg=COLORS["bg"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(queue_shell, orient="vertical", command=self.canvas.yview, style="Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.items_frame = tk.Frame(self.canvas, bg=COLORS["bg"])
        self.canvas_window = self.canvas.create_window((0, 0), window=self.items_frame, anchor="nw")
        self.items_frame.bind("<Configure>", self._on_items_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _event: self.root.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda _event: self.root.unbind_all("<MouseWheel>"))

        footer = tk.Frame(self.root, bg=COLORS["bg"], padx=12, pady=9)
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        tk.Label(
            footer,
            textvariable=self.foreground_var,
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            footer,
            textvariable=self.footer_var,
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

    def _build_controls(self, parent: tk.Frame) -> None:
        top = tk.Frame(parent, bg=COLORS["bg"])
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        tk.Label(
            top,
            textvariable=self.counts_var,
            bg=COLORS["bg"],
            fg=COLORS["ink"],
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._button(top, "Refresh", lambda: self._manual_refresh(), variant="light", width=8).grid(
            row=0, column=1, sticky="e"
        )

        mode_row = tk.Frame(parent, bg=COLORS["bg"])
        mode_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        mode_row.columnconfigure(3, weight=1)
        for column, mode in enumerate(("dry_run", "confirm", "auto")):
            tk.Radiobutton(
                mode_row,
                text=mode.replace("_", " "),
                value=mode,
                variable=self.mode_var,
                command=self._apply_controls,
                indicatoron=False,
                padx=10,
                pady=5,
                bd=1,
                relief="solid",
                selectcolor=COLORS["accent_soft"],
                bg=COLORS["surface"],
                fg=COLORS["ink"],
                activebackground=COLORS["accent_soft"],
                activeforeground=COLORS["ink"],
                font=("Segoe UI", 8),
            ).grid(row=0, column=column, sticky="w", padx=(0, 6))
        tk.Checkbutton(
            mode_row,
            text="Send enabled",
            variable=self.send_enabled_var,
            command=self._apply_controls,
            bg=COLORS["bg"],
            fg=COLORS["ink"],
            activebackground=COLORS["bg"],
            font=("Segoe UI", 8),
        ).grid(row=0, column=3, sticky="e")

        driver_row = tk.Frame(parent, bg=COLORS["bg"])
        driver_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        driver_row.columnconfigure(1, weight=1)
        tk.Label(driver_row, text="Driver", bg=COLORS["bg"], fg=COLORS["muted"], font=("Segoe UI", 8)).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.driver_combo = ttk.Combobox(driver_row, textvariable=self.driver_var, state="readonly", height=5)
        self.driver_combo.grid(row=0, column=1, sticky="ew")
        self.driver_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_controls())
        tk.Checkbutton(
            driver_row,
            text="Dock",
            variable=self.dock_enabled_var,
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            activebackground=COLORS["bg"],
            font=("Segoe UI", 8),
        ).grid(row=0, column=2, sticky="e", padx=(8, 0))

    def _render(self, state: dict[str, Any], *, force: bool = False) -> None:
        self._sync_header(state)
        fingerprint = sidebar_state_fingerprint(state)
        if not force and fingerprint == self._last_fingerprint:
            return
        self._last_fingerprint = fingerprint
        self._render_queue(flatten_queue_items(state))

    def _sync_header(self, state: dict[str, Any]) -> None:
        readiness = state.get("readiness", {}) if isinstance(state.get("readiness"), dict) else {}
        summary = readiness.get("summary", {}) if isinstance(readiness.get("summary"), dict) else {}
        config = state.get("config", {}) if isinstance(state.get("config"), dict) else {}
        status = str(readiness.get("status", "unknown"))
        blockers = int(summary.get("blockers", 0) or 0)
        warnings = int(summary.get("warnings", 0) or 0)
        counts = queue_counts(state)

        self.status_var.set(status.upper())
        self.status_detail_var.set(
            f"blockers {blockers} / warnings {warnings} / driver {config.get('send_driver', '')}"
        )
        self.counts_var.set(
            f"Pending {counts.get('pending', 0)}  Approved {counts.get('approved', 0)}  Failed {counts.get('failed', 0)}"
        )
        self.footer_var.set(f"Rejected {counts.get('rejected', 0)} / Sent {counts.get('sent', 0)}")

        self._syncing_controls = True
        try:
            self.mode_var.set(str(config.get("mode", "dry_run")))
            self.send_enabled_var.set(bool(config.get("send_enabled", False)))
            self.driver_var.set(str(config.get("send_driver", "")))
            drivers = _driver_names_from_state(state)
            if drivers:
                self.driver_combo.configure(values=drivers)
            elif self.driver_var.get():
                self.driver_combo.configure(values=[self.driver_var.get()])
        finally:
            self._syncing_controls = False

        foreground = _foreground_from_state(state)
        foreground_title = str(foreground.get("title", "")).strip()
        foreground_process = str(foreground.get("process_name", "")).strip()
        if foreground_title or foreground_process:
            self.foreground_var.set(f"Foreground: {foreground_title or '(untitled)'} {foreground_process}".strip())

    def _render_queue(self, items: list[dict[str, Any]]) -> None:
        assert self.items_frame is not None
        assert self.canvas is not None
        scroll_top = self.canvas.yview()[0] if self.canvas.winfo_exists() else 0
        for child in self.items_frame.winfo_children():
            child.destroy()
        if not items:
            self._empty_state(self.items_frame)
        else:
            for row, item in enumerate(items):
                self._queue_item(self.items_frame, row, item)
        self.root.update_idletasks()
        self.canvas.yview_moveto(scroll_top)

    def _empty_state(self, parent: tk.Frame) -> None:
        box = tk.Frame(parent, bg=COLORS["surface"], highlightthickness=1, highlightbackground=COLORS["line"])
        box.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(24, 8))
        box.columnconfigure(0, weight=1)
        tk.Label(
            box,
            text="Queue is clear",
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="ew", pady=(28, 4))
        tk.Label(
            box,
            text="Replies will appear here before they are approved, rejected, or sent.",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            wraplength=320,
            justify="center",
        ).grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 28))

    def _queue_item(self, parent: tk.Frame, row: int, item: dict[str, Any]) -> None:
        status = str(item.get("status", "")).lower()
        fg, soft = STATUS_COLORS.get(status, (COLORS["muted"], COLORS["surface_alt"]))
        reply = item.get("reply", {}) if isinstance(item.get("reply"), dict) else {}
        queue_id = str(item.get("queue_id", ""))
        conversation_id = str(reply.get("conversation_id", ""))
        text = _compact(str(reply.get("text", "")), 520)
        created_at = _short_time(str(item.get("created_at") or reply.get("created_at") or ""))

        card = tk.Frame(parent, bg=COLORS["surface"], highlightthickness=1, highlightbackground=COLORS["line"])
        card.grid(row=row, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))
        card.columnconfigure(1, weight=1)
        tk.Frame(card, bg=fg, width=4).grid(row=0, column=0, rowspan=4, sticky="nsw")

        title_row = tk.Frame(card, bg=COLORS["surface"], padx=10, pady=(9, 0))
        title_row.grid(row=0, column=1, sticky="ew")
        title_row.columnconfigure(1, weight=1)
        self._pill(title_row, status or "queued", fg=fg, bg=soft).grid(row=0, column=0, sticky="w")
        tk.Label(
            title_row,
            text=created_at,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
        ).grid(row=0, column=2, sticky="e")

        tk.Label(
            card,
            text=f"conversation {conversation_id[:12] or '-'}",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=1, column=1, sticky="ew", padx=10, pady=(5, 0))

        tk.Label(
            card,
            text=text or "(empty reply)",
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            font=("Segoe UI", 9),
            wraplength=340,
            justify="left",
            anchor="w",
        ).grid(row=2, column=1, sticky="ew", padx=10, pady=(7, 8))

        actions = tk.Frame(card, bg=COLORS["surface"], padx=10, pady=(0, 10))
        actions.grid(row=3, column=1, sticky="ew")
        actions.columnconfigure(0, weight=1)
        tk.Label(
            actions,
            text=f"id {queue_id[:10]}",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
        ).grid(row=0, column=0, sticky="w")
        button_box = tk.Frame(actions, bg=COLORS["surface"])
        button_box.grid(row=0, column=1, sticky="e")
        if status == "pending":
            self._button(button_box, "Approve", lambda qid=queue_id: self._queue_action(qid, "approve"), "primary").grid(
                row=0, column=0, padx=(0, 6)
            )
            self._button(button_box, "Reject", lambda qid=queue_id: self._queue_action(qid, "reject"), "danger").grid(
                row=0, column=1
            )
        elif status == "approved":
            self._button(button_box, "Send 3s", lambda qid=queue_id: self._send_after_delay(qid), "primary").grid(
                row=0, column=0, padx=(0, 6)
            )
            self._button(button_box, "Reject", lambda qid=queue_id: self._queue_action(qid, "reject"), "danger").grid(
                row=0, column=1
            )

    def _queue_action(self, queue_id: str, action: str) -> None:
        if not queue_id:
            return
        try:
            sidebar_queue_action(self.data_dir, action, queue_id, {"reviewer": "sidebar_window"})
            self.status_detail_var.set(f"{action} completed")
            self._manual_refresh()
        except Exception as exc:
            self.status_var.set("ACTION FAILED")
            self.status_detail_var.set(f"{action}: {type(exc).__name__}: {exc}")

    def _send_after_delay(self, queue_id: str) -> None:
        def worker() -> None:
            for remaining in [3, 2, 1]:
                self.root.after(
                    0,
                    lambda value=remaining: self.status_detail_var.set(
                        f"Switch focus to the target WeChat chat: {value}s"
                    ),
                )
                time.sleep(1)
            self.root.after(0, lambda: self._queue_action(queue_id, "send-approved"))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_controls(self) -> None:
        if self._syncing_controls:
            return
        try:
            update_sidebar_controls(
                self.data_dir,
                {
                    "mode": self.mode_var.get(),
                    "send_enabled": self.send_enabled_var.get(),
                    "send_driver": self.driver_var.get(),
                },
            )
            self.status_detail_var.set("Controls updated")
            self._manual_refresh()
        except Exception as exc:
            self.status_var.set("CONTROL FAILED")
            self.status_detail_var.set(f"{type(exc).__name__}: {exc}")

    def _manual_refresh(self) -> None:
        self._cancel_refresh()
        self.refresh(force=True)

    def _schedule_refresh(self) -> None:
        self._cancel_refresh()
        self._refresh_after_id = self.root.after(self.poll_interval_ms, self.refresh)

    def _cancel_refresh(self) -> None:
        if self._refresh_after_id is None:
            return
        try:
            self.root.after_cancel(self._refresh_after_id)
        except tk.TclError:
            pass
        self._refresh_after_id = None

    def _dock_near_wechat(self) -> None:
        if not self.dock_enabled_var.get():
            return
        foreground = foreground_window_info()
        target = None
        if _looks_like_wechat_foreground(foreground):
            target = foreground
        else:
            windows = Win32WindowProbe(include_invisible=False).find_wechat_windows()
            if windows:
                window = windows[0]
                target = {
                    "right": window.right,
                    "left": window.left,
                    "top": window.top,
                    "title": window.title,
                    "process_name": window.process_name,
                }
        if not target:
            return
        left = int(target.get("right", 0) or 0) + 8
        top = int(target.get("top", 80) or 80)
        geometry = f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{max(left, 0)}+{max(top, 0)}"
        if geometry != self._last_geometry:
            self.root.geometry(geometry)
            self._last_geometry = geometry

    def _close(self) -> None:
        self._cancel_refresh()
        self.root.destroy()

    def _on_items_configure(self, _event: tk.Event) -> None:
        assert self.canvas is not None
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        if self.canvas is not None and self.canvas_window is not None:
            self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.canvas is not None:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _pill(self, parent: tk.Widget, text: str | tk.StringVar, *, fg: str, bg: str) -> tk.Label:
        text_options: dict[str, Any]
        if isinstance(text, tk.StringVar):
            text_options = {"textvariable": text}
        else:
            text_options = {"text": text}
        return tk.Label(
            parent,
            bg=bg,
            fg=fg,
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=3,
            **text_options,
        )

    def _button(self, parent: tk.Widget, text: str, command: Any, variant: str, width: int | None = None) -> tk.Button:
        palettes = {
            "primary": (COLORS["accent"], "white", "#115e59"),
            "danger": (COLORS["danger_soft"], COLORS["danger"], "#ffd9d5"),
            "light": (COLORS["surface"], COLORS["ink"], COLORS["surface_alt"]),
        }
        bg, fg, active = palettes.get(variant, palettes["light"])
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width or 9,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            relief="flat",
            bd=0,
            padx=8,
            pady=5,
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        )


def _driver_names_from_state(state: dict[str, Any]) -> list[str]:
    probe = state.get("driver_probe", {}) if isinstance(state.get("driver_probe"), dict) else {}
    raw = probe.get("registered_send_drivers", [])
    names = [
        str(item.get("name", "")).strip()
        for item in raw
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    configured = str(state.get("config", {}).get("send_driver", "")).strip() if isinstance(state.get("config"), dict) else ""
    if configured and configured not in names:
        names.insert(0, configured)
    return names


def _foreground_from_state(state: dict[str, Any]) -> dict[str, Any]:
    probe = state.get("driver_probe", {}) if isinstance(state.get("driver_probe"), dict) else {}
    foreground = probe.get("foreground", {}) if isinstance(probe.get("foreground"), dict) else {}
    return dict(foreground)


def _looks_like_wechat_foreground(foreground: dict[str, Any]) -> bool:
    process = str(foreground.get("process_name", "")).lower()
    title = str(foreground.get("title", ""))
    return process in {"wechat.exe", "weixin.exe", "wechatappex.exe"} or "微信" in title


def _compact(text: str, max_chars: int) -> str:
    normalized = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "..."


def _short_time(value: str) -> str:
    if "T" in value:
        value = value.split("T", 1)[1]
    if "+" in value:
        value = value.split("+", 1)[0]
    if "." in value:
        value = value.split(".", 1)[0]
    return value[:8]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
