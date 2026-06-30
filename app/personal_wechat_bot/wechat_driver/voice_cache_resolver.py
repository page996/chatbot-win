from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUDIO_CACHE_SUFFIXES = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".wma",
    ".amr",
    ".silk",
}


@dataclass(frozen=True)
class VoiceCacheCandidate:
    path: str
    suffix: str
    size: int
    modified_at: str
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class VoiceCacheResolveResult:
    status: str
    path: str = ""
    reason: str = ""
    candidates_scanned: int = 0
    candidate_roots: tuple[str, ...] = ()
    candidates: tuple[VoiceCacheCandidate, ...] = ()
    blockers: tuple[str, ...] = ("wechat_db_decryption_not_supported",)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [asdict(item) for item in self.candidates]
        return payload


class WeChatVoiceCacheResolver:
    """Best-effort resolver for readable WeChat voice cache files.

    This class intentionally does not decrypt WeChat databases or inspect
    process memory. It only searches normal files under caller-provided roots.
    """

    def __init__(
        self,
        roots: list[str | Path],
        *,
        allowed_extensions: list[str] | None = None,
        max_bytes: int = 20 * 1024 * 1024,
        time_window_seconds: int = 10 * 60,
        max_scan_files: int = 2000,
        suffixes: set[str] | None = None,
    ):
        configured_suffixes = {item.lower() for item in allowed_extensions or []}
        supported = {item.lower() for item in (suffixes or AUDIO_CACHE_SUFFIXES)}
        self.suffixes = supported & configured_suffixes if configured_suffixes else supported
        self.roots = _dedupe_paths(Path(item).resolve() for item in roots)
        self.max_bytes = max_bytes
        self.time_window_seconds = max(0, time_window_seconds)
        self.max_scan_files = max(1, max_scan_files)

    def resolve(
        self,
        voice: dict[str, Any],
        *,
        chat_title: str = "",
        observed_at: str = "",
    ) -> VoiceCacheResolveResult:
        existing_roots = [root for root in self.roots if root.exists() and root.is_dir()]
        root_refs = tuple(str(root) for root in self.roots)
        if not self.roots:
            return VoiceCacheResolveResult(
                status="not_configured",
                reason="wechat_voice_roots_empty",
                candidate_roots=root_refs,
            )
        if not existing_roots:
            return VoiceCacheResolveResult(
                status="not_found",
                reason="configured_voice_roots_missing",
                candidate_roots=root_refs,
            )
        if not self.suffixes:
            return VoiceCacheResolveResult(
                status="blocked",
                reason="no_supported_audio_suffixes_configured",
                candidate_roots=tuple(str(root) for root in existing_roots),
            )

        hints = _voice_hints(voice)
        chat_hint = (chat_title or str(voice.get("chat_title", ""))).strip()
        observed = _parse_time(observed_at or str(voice.get("observed_at", "")))
        if observed is None and not hints:
            return VoiceCacheResolveResult(
                status="blocked",
                reason="insufficient_voice_cache_hints",
                candidate_roots=tuple(str(root) for root in existing_roots),
            )

        scanned = 0
        ranked: list[VoiceCacheCandidate] = []
        for root in existing_roots:
            for path in _iter_files(root):
                if scanned >= self.max_scan_files:
                    break
                scanned += 1
                candidate = self._candidate(path, hints=hints, chat_title=chat_hint, observed_at=observed)
                if candidate is not None:
                    ranked.append(candidate)
            if scanned >= self.max_scan_files:
                break

        ranked.sort(key=lambda item: item.score, reverse=True)
        preview = tuple(ranked[:10])
        if not ranked:
            return VoiceCacheResolveResult(
                status="not_found",
                reason="no_matching_readable_audio_cache",
                candidates_scanned=scanned,
                candidate_roots=tuple(str(root) for root in existing_roots),
                candidates=preview,
            )
        best = ranked[0]
        if best.score <= 0:
            return VoiceCacheResolveResult(
                status="not_found",
                reason="no_candidate_with_matching_hints",
                candidates_scanned=scanned,
                candidate_roots=tuple(str(root) for root in existing_roots),
                candidates=preview,
            )
        return VoiceCacheResolveResult(
            status="resolved",
            path=best.path,
            reason="matched_readable_audio_cache",
            candidates_scanned=scanned,
            candidate_roots=tuple(str(root) for root in existing_roots),
            candidates=preview,
        )

    def _candidate(
        self,
        path: Path,
        *,
        hints: set[str],
        chat_title: str,
        observed_at: datetime | None,
    ) -> VoiceCacheCandidate | None:
        suffix = path.suffix.lower()
        if suffix not in self.suffixes:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size <= 0 or stat.st_size > self.max_bytes:
            return None
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        score = 0.0
        reasons: list[str] = []
        lower_path = str(path).lower()
        lower_name = path.name.lower()

        for hint in hints:
            if hint and hint in lower_name:
                score += 100.0
                reasons.append(f"name_hint:{hint}")
            elif hint and hint in lower_path:
                score += 40.0
                reasons.append(f"path_hint:{hint}")
        if chat_title and chat_title.lower() in lower_path:
            score += 8.0
            reasons.append("chat_title_path_hint")
        if observed_at is not None:
            diff = abs((modified - observed_at).total_seconds())
            if diff <= self.time_window_seconds:
                window = max(float(self.time_window_seconds), 1.0)
                score += 25.0 * (1.0 - (diff / window)) + 1.0
                reasons.append(f"time_window:{int(diff)}s")
            elif not hints:
                return None
        if not reasons:
            return None
        return VoiceCacheCandidate(
            path=str(path),
            suffix=suffix,
            size=stat.st_size,
            modified_at=modified.isoformat(),
            score=round(score, 3),
            reasons=tuple(reasons),
        )


def default_wechat_voice_roots() -> list[Path]:
    """Return existing broad WeChat data roots for diagnostics only."""

    home = Path.home()
    candidates = [
        home / "Documents" / "WeChat Files",
        home / "Documents" / "Weixin Files",
    ]
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            candidates.append(Path(env_value) / "Tencent" / "WeChat")
    return _dedupe_paths(path.resolve() for path in candidates if str(path) and path.exists() and path.is_dir())


def voice_cache_capability(roots: list[str | Path], allowed_extensions: list[str]) -> dict[str, Any]:
    resolver = WeChatVoiceCacheResolver(roots, allowed_extensions=allowed_extensions)
    existing = [root for root in resolver.roots if root.exists() and root.is_dir()]
    return {
        "supported": True,
        "mode": "readable_file_cache_only",
        "db_decryption": "not_supported_by_design",
        "configured_roots": [str(root) for root in resolver.roots],
        "existing_roots": [str(root) for root in existing],
        "supported_suffixes": sorted(resolver.suffixes),
        "automatic_fallback_enabled": bool(existing),
    }


def _voice_hints(voice: dict[str, Any]) -> set[str]:
    keys = (
        "audio_name",
        "name",
        "filename",
        "file_name",
        "msg_id",
        "message_id",
        "local_id",
        "server_id",
        "client_msg_id",
    )
    hints: set[str] = set()
    for key in keys:
        value = str(voice.get(key, "")).strip().lower()
        if value:
            hints.add(Path(value).name.lower())
            hints.add(Path(value).stem.lower())
            hints.add(value)
    audio = voice.get("audio")
    if isinstance(audio, dict):
        hints.update(_voice_hints(audio))
    return {item for item in hints if item}


def _parse_time(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iter_files(root: Path):
    try:
        iterator = root.rglob("*")
        for path in iterator:
            try:
                if path.is_file():
                    yield path
            except OSError:
                continue
    except OSError:
        return


def _dedupe_paths(paths) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return resolved
