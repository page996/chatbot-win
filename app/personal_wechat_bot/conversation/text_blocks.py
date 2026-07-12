from __future__ import annotations

from typing import Any, Literal


BlockChannel = Literal["authored", "evidence", "derived", "hidden"]


def block_channel(block: Any) -> BlockChannel:
    if not isinstance(block, dict) or not str(block.get("text") or "").strip():
        return "hidden"
    kind = str(block.get("kind") or "text").strip().lower()
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    declared = str(metadata.get("context_channel") or "").strip().lower()
    if kind.startswith("annotation:") or declared == "evidence":
        return "evidence"
    if kind.startswith(("attachment:", "control:")):
        return "hidden"
    if metadata.get("visible_in_context") is False:
        return "hidden"
    if kind == "file:analysis" or declared == "derived":
        return "derived"
    if kind in {"text", "reply", "voice:transcript"} or declared == "authored":
        return "authored"
    # Legacy rows without a kind were authored text. Unknown named kinds fail closed.
    return "authored" if not str(block.get("kind") or "").strip() else "derived"


def is_authored_block(block: Any) -> bool:
    return block_channel(block) == "authored"


def is_evidence_block(block: Any) -> bool:
    return block_channel(block) == "evidence"


def is_prompt_visible_block(block: Any) -> bool:
    return block_channel(block) in {"authored", "derived"}


def block_text(block: Any) -> str:
    return str(block.get("text") or "").strip() if isinstance(block, dict) else ""


def evidence_metadata(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {}
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    return {
        "kind": str(block.get("kind") or "annotation:web"),
        "source_ref": str(block.get("source_ref") or ""),
        **dict(metadata),
    }
