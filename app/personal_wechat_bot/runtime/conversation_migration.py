from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# Directories whose top-level entries are named by conversation_id.
CONVERSATION_DIR_NAMES = (
    "conversation_ledgers",
    "conversation_channels",
    "conversation_sessions",
    "conversation_context",
)
FILE_WORKSPACE_DIR = "file_workspace"
# Text files inside a migrated tree that may embed the old conversation_id as a
# path segment or field value; safe to string-replace because the id is a
# 24-hex-char hash that will not collide with unrelated content.
_REWRITE_SUFFIXES = {".json", ".jsonl", ".md", ".txt"}


@dataclass
class ConversationMigration:
    old_id: str
    new_id: str
    chat_title: str = ""
    talker_id: str = ""


@dataclass
class MigrationItemReport:
    old_id: str
    new_id: str
    moved_dirs: list[str] = field(default_factory=list)
    rewritten_files: int = 0
    reidentified_entries: int = 0
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_id": self.old_id,
            "new_id": self.new_id,
            "moved_dirs": self.moved_dirs,
            "rewritten_files": self.rewritten_files,
            "reidentified_entries": self.reidentified_entries,
            "conflicts": self.conflicts,
            "notes": self.notes,
        }


@dataclass
class MigrationReport:
    dry_run: bool
    data_dir: str
    items: list[MigrationItemReport] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "data_dir": self.data_dir,
            "migrated_count": len(self.items),
            "skipped_count": len(self.skipped),
            "items": [item.to_dict() for item in self.items],
            "skipped": self.skipped,
        }


def _entry_id(conversation_id: str, message_id: str) -> str:
    return hashlib.sha256(f"{conversation_id}:{message_id}".encode("utf-8")).hexdigest()[:24]


def load_migration_map(path: str | Path) -> list[ConversationMigration]:
    """Load a migration map JSON.

    Expected shape:
        {"migrations": [
            {"old_id": "...", "new_id": "...", "chat_title": "PAGE", "talker_id": "wxid_x"},
            ...
        ]}

    ``new_id`` may be omitted when ``talker_id`` is given; it is then derived as
    conversation_id_for(conversation_type, talker_id). To keep this module free
    of import cycles the caller can pre-fill new_id; otherwise old_id is kept
    (a no-op, surfaced as a skip).
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_items = payload.get("migrations") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raise ValueError("migration map must contain a 'migrations' list")
    migrations: list[ConversationMigration] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        old_id = str(item.get("old_id") or "").strip()
        new_id = str(item.get("new_id") or "").strip()
        if not old_id or not new_id:
            continue
        migrations.append(
            ConversationMigration(
                old_id=old_id,
                new_id=new_id,
                chat_title=str(item.get("chat_title") or "").strip(),
                talker_id=str(item.get("talker_id") or "").strip(),
            )
        )
    return migrations


def migrate_conversations(
    data_dir: str | Path,
    migrations: Iterable[ConversationMigration],
    *,
    dry_run: bool = True,
) -> MigrationReport:
    """Rehash conversations from an old id scheme to a new one.

    WARNING: This tool predates the human-readable directory naming
    (``chat_title_hashPrefix``, see conversation/segment.py). It assumes each
    top-level directory under CONVERSATION_DIR_NAMES is named by the raw
    conversation_id. Under the current layout that assumption no longer holds,
    so a move-by-id here will not find the readable dirs. It is retained only
    for the legacy hash-only-id migration path; do NOT use it against a store
    created with the readable naming without first updating the segment logic.
    """
    root = Path(data_dir)
    report = MigrationReport(dry_run=dry_run, data_dir=str(root))
    for migration in migrations:
        item = _migrate_one(root, migration, dry_run=dry_run, report=report)
        if item is not None:
            report.items.append(item)
    return report


def _migrate_one(
    root: Path,
    migration: ConversationMigration,
    *,
    dry_run: bool,
    report: MigrationReport,
) -> MigrationItemReport | None:
    old_id = migration.old_id
    new_id = migration.new_id
    if old_id == new_id:
        report.skipped.append({"old_id": old_id, "reason": "old_id == new_id (no change)"})
        return None
    item = MigrationItemReport(old_id=old_id, new_id=new_id)

    # 1. Move conversation_id-named directories.
    move_targets: list[tuple[Path, Path]] = []
    for dir_name in (*CONVERSATION_DIR_NAMES, FILE_WORKSPACE_DIR):
        src = root / dir_name / old_id
        dst = root / dir_name / new_id
        if not src.exists():
            continue
        if dst.exists():
            item.conflicts.append(f"{dir_name}/{new_id} already exists; skipped move")
            continue
        move_targets.append((src, dst))

    if not move_targets:
        item.notes.append("no source directories found for old_id")
        report.skipped.append({"old_id": old_id, "reason": "no source directories"})
        return None

    for src, dst in move_targets:
        item.moved_dirs.append(f"{src.parent.name}/{old_id} -> {dst.parent.name}/{new_id}")
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    # 2. Rewrite embedded old_id strings and recompute ledger entry ids inside
    #    the (now moved) new_id directories.
    for dir_name in (*CONVERSATION_DIR_NAMES, FILE_WORKSPACE_DIR):
        target_dir = root / dir_name / (new_id if not dry_run else old_id)
        # In dry-run the dirs have not moved, so inspect the source location.
        if not target_dir.exists():
            continue
        rewritten, reidentified = _rewrite_tree(
            target_dir,
            old_id=old_id,
            new_id=new_id,
            is_ledger=(dir_name == "conversation_ledgers"),
            dry_run=dry_run,
        )
        item.rewritten_files += rewritten
        item.reidentified_entries += reidentified

    return item


def _rewrite_tree(
    tree: Path,
    *,
    old_id: str,
    new_id: str,
    is_ledger: bool,
    dry_run: bool,
) -> tuple[int, int]:
    rewritten = 0
    reidentified = 0
    for path in tree.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _REWRITE_SUFFIXES:
            continue
        if is_ledger and path.name == "messages.jsonl":
            changed, count = _rewrite_ledger_messages(path, old_id, new_id, dry_run=dry_run)
            if changed:
                rewritten += 1
                reidentified += count
            continue
        if _rewrite_text_file(path, old_id, new_id, dry_run=dry_run):
            rewritten += 1
    return rewritten, reidentified


def _rewrite_text_file(path: Path, old_id: str, new_id: str, *, dry_run: bool) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if old_id not in text:
        return False
    if not dry_run:
        path.write_text(text.replace(old_id, new_id), encoding="utf-8")
    return True


def _rewrite_ledger_messages(path: Path, old_id: str, new_id: str, *, dry_run: bool) -> tuple[bool, int]:
    """Rewrite a ledger messages.jsonl: swap conversation_id and old-id path
    segments, and recompute entry_id = hash(new_conv:message_id).

    message_id is preserved (ledger entries do not store raw_id, so it cannot be
    recomputed to align with future captures; keeping it stable keeps the
    ledger self-consistent)."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False, 0
    changed = False
    reidentified = 0
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(entry, dict):
            before = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            # Swap any embedded old-id path segments / field values first.
            entry = _swap_ids_recursive(entry, old_id, new_id)
            entry["conversation_id"] = new_id
            message_id = str(entry.get("message_id") or "")
            if message_id:
                entry["entry_id"] = _entry_id(new_id, message_id)
                reidentified += 1
            after = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            if before != after:
                changed = True
            out.append(json.dumps(entry, ensure_ascii=False))
        else:
            out.append(line)
    if changed and not dry_run:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        tmp.replace(path)
    return changed, reidentified


def _swap_ids_recursive(value: Any, old_id: str, new_id: str) -> Any:
    if isinstance(value, str):
        return value.replace(old_id, new_id) if old_id in value else value
    if isinstance(value, list):
        return [_swap_ids_recursive(item, old_id, new_id) for item in value]
    if isinstance(value, dict):
        return {key: _swap_ids_recursive(item, old_id, new_id) for key, item in value.items()}
    return value
