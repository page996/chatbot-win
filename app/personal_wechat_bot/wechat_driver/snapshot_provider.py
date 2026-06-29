from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Protocol


class SnapshotProvider(Protocol):
    def read_text(self) -> str: ...


class FileSnapshotProvider:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read_text(self) -> str:
        return self.path.read_text(encoding="utf-8")


class StaticSnapshotProvider:
    def __init__(self, text: str):
        self.text = text

    def read_text(self) -> str:
        return self.text


class WindowsClipboardSnapshotProvider:
    CF_UNICODETEXT = 13

    def read_text(self) -> str:
        if sys.platform != "win32":
            return ""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
            return ""
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()


@dataclass(frozen=True)
class AutomationTextNode:
    name: str
    control_type: str = ""
    depth: int = 0


class WindowsUIAutomationSnapshotProvider:
    """Read visible text names from a Windows UI Automation tree.

    This provider is read-only. It does not click, focus, type, or send input.
    """

    UIA_NAME_PROPERTY_ID = 30005
    UIA_CONTROL_TYPE_PROPERTY_ID = 30003
    UIA_TREE_SCOPE_DESCENDANTS = 0x4
    UIA_TREE_SCOPE_SUBTREE = 0x7

    def __init__(
        self,
        title_keywords: list[str] | None = None,
        max_nodes: int = 500,
        max_depth: int = 8,
        collector: "AutomationCollector | None" = None,
    ):
        self.title_keywords = title_keywords or ["微信", "WeChat"]
        self.max_nodes = max(1, max_nodes)
        self.max_depth = max(1, max_depth)
        self.collector = collector or Win32UIAutomationCollector(self.title_keywords)

    def read_text(self) -> str:
        nodes = self.collector.collect_text_nodes(max_nodes=self.max_nodes, max_depth=self.max_depth)
        return format_automation_text_nodes(nodes)


class AutomationCollector(Protocol):
    def collect_text_nodes(self, max_nodes: int, max_depth: int) -> list[AutomationTextNode]: ...


class Win32UIAutomationCollector:
    CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
    IID_IUIAUTOMATION = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"

    def __init__(self, title_keywords: list[str]):
        self.title_keywords = title_keywords

    def collect_text_nodes(self, max_nodes: int, max_depth: int) -> list[AutomationTextNode]:
        if sys.platform != "win32":
            return []
        try:
            import comtypes.client  # type: ignore[import-not-found]
        except Exception:
            return []

        try:
            automation = comtypes.client.CreateObject(self.CLSID_CUIAUTOMATION, interface=None)
            root = automation.GetRootElement()
            condition = automation.CreateTrueCondition()
            elements = root.FindAll(self.UIA_TREE_SCOPE_DESCENDANTS, condition)
        except Exception:
            return []

        nodes: list[AutomationTextNode] = []
        total = min(int(elements.Length), max_nodes * 4)
        for index in range(total):
            if len(nodes) >= max_nodes:
                break
            try:
                element = elements.GetElement(index)
                name = str(element.GetCurrentPropertyValue(self.UIA_NAME_PROPERTY_ID) or "").strip()
                control_type = str(element.GetCurrentPropertyValue(self.UIA_CONTROL_TYPE_PROPERTY_ID) or "").strip()
            except Exception:
                continue
            if not name:
                continue
            if _looks_like_wechat_related(name, self.title_keywords) or nodes:
                nodes.append(AutomationTextNode(name=name, control_type=control_type, depth=0))
        return nodes[:max_nodes]


def format_automation_text_nodes(nodes: list[AutomationTextNode]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        text = " ".join(node.name.split())
        if not text or text in seen:
            continue
        seen.add(text)
        indent = "  " * max(0, node.depth)
        label = f" [{node.control_type}]" if node.control_type else ""
        lines.append(f"{indent}{text}{label}")
    return "\n".join(lines)


def _looks_like_wechat_related(name: str, keywords: list[str]) -> bool:
    folded = name.lower()
    return any(keyword.lower() in folded for keyword in keywords)
