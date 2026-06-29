from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.permissions import validate_readable_file
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


@dataclass(frozen=True)
class IncomingAttachment:
    path: str
    original_name: str = ""
    kind: str = "file"
    source: str = "backend_event_attachment"


class AttachmentPipeline:
    """Validate, stage, parse, and index incoming files.

    Drivers should provide structured attachment events and let this pipeline
    own the middle-layer file lifecycle. That keeps WeChat/file-source adapters
    thin and makes future frontend monitor events use the same path.
    """

    def __init__(
        self,
        *,
        file_index: FileIndex,
        file_workspace: FileWorkspace,
        attachment_parser: BackendAttachmentParser,
        allowed_input_roots: list[Path],
        allowed_extensions: list[str],
        max_input_bytes: int,
    ):
        self.file_index = file_index
        self.file_workspace = file_workspace
        self.attachment_parser = attachment_parser
        self.allowed_input_roots = allowed_input_roots
        self.allowed_extensions = allowed_extensions
        self.max_input_bytes = max_input_bytes

    def process(
        self,
        attachment: IncomingAttachment,
        *,
        conversation_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        try:
            safe_path = validate_readable_file(
                attachment.path,
                self.allowed_input_roots,
                self.allowed_extensions,
                self.max_input_bytes,
            )
            staged = self.file_workspace.stage_file(
                safe_path,
                conversation_id=conversation_id,
                session_id=session_id,
                original_name=attachment.original_name or safe_path.name,
                kind=attachment.kind,
                source=attachment.source,
            )
            file_id = self.file_index.add(
                staged.staged_path,
                source="file_workspace",
                original_name=attachment.original_name or safe_path.name,
            )
            parse_result = self.file_workspace.parse_or_get_cached(staged, self.attachment_parser)
            return {
                "status": "indexed",
                "file_id": file_id,
                "name": attachment.original_name or safe_path.name,
                "kind": attachment.kind,
                "suffix": safe_path.suffix.lower(),
                "workspace": {
                    "conversation_id": staged.conversation_id,
                    "session_id": staged.session_id,
                    "workspace_dir": staged.workspace_dir,
                    "staged_path": staged.staged_path,
                    "manifest_path": staged.manifest_path,
                    "derived_dir": staged.derived_dir,
                    "outputs_dir": staged.outputs_dir,
                    "sha256": staged.sha256,
                },
                "parse": {
                    "status": parse_result.status,
                    "kind": parse_result.kind,
                    "summary": parse_result.summary,
                    "text": parse_result.text,
                    "error": parse_result.error,
                },
            }
        except (FileNotFoundError, PermissionError) as exc:
            return {
                "status": "blocked",
                "name": attachment.original_name or Path(attachment.path).name,
                "kind": attachment.kind,
                "reason": f"{type(exc).__name__}: {exc}",
            }
