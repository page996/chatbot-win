from __future__ import annotations

import os
import re
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.personal_wechat_bot.config.schema import ProviderConfig


@dataclass(frozen=True)
class ApiKeyRef:
    ref: str
    source: str
    available: bool


class ApiKeyPool:
    def __init__(self, provider: ProviderConfig, data_dir: str | Path = "data"):
        self.provider = provider
        self.data_dir = Path(data_dir)

    def refs(self) -> list[ApiKeyRef]:
        refs: list[ApiKeyRef] = []
        env_names = self._env_names()
        for name in env_names:
            refs.append(ApiKeyRef(ref=name, source="env", available=bool(os.environ.get(name))))
        for item in self._file_entries():
            if item.ref not in {ref.ref for ref in refs}:
                refs.append(item)
        return refs

    def available_count(self) -> int:
        return sum(1 for item in self.refs() if item.available)

    def key_for_ref(self, ref: str) -> str | None:
        env_value = os.environ.get(ref)
        if env_value:
            return env_value
        for item_ref, value in self._file_secret_values().items():
            if item_ref == ref:
                return value
        return None

    def default_key(self) -> str | None:
        for item in self.refs():
            value = self.key_for_ref(item.ref)
            if value:
                return value
        return None

    def _env_names(self) -> list[str]:
        names = list(self.provider.api_key_env_pool)
        if self.provider.api_key_env and self.provider.api_key_env not in names:
            names.insert(0, self.provider.api_key_env)
        return names

    def _file_entries(self) -> list[ApiKeyRef]:
        entries: list[ApiKeyRef] = []
        for line in self._key_file_lines():
            parsed = _parse_key_file_line(line)
            if parsed is None:
                continue
            ref, source, value = parsed
            available = bool(os.environ.get(ref)) if source == "file_env" else bool(value)
            entries.append(ApiKeyRef(ref=ref, source=source, available=available))
        return entries

    def _file_secret_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self._key_file_lines():
            parsed = _parse_key_file_line(line)
            if parsed is None:
                continue
            ref, source, value = parsed
            if source == "file_secret" and value:
                values[ref] = value
        return values

    def _key_file_lines(self) -> list[str]:
        if not self.provider.api_key_file:
            return []
        path = Path(self.provider.api_key_file)
        if not path.is_absolute():
            path = self.data_dir / path
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return text.splitlines()


class ConversationKeyAssigner:
    def __init__(self, pool: ApiKeyPool):
        self.pool = pool
        self._assignments: dict[str, list[str]] = {}

    def assign(self, conversation_id: str, slots: int = 1) -> list[str]:
        if conversation_id in self._assignments:
            return list(self._assignments[conversation_id])
        refs = self.pool.refs()
        if not refs:
            self._assignments[conversation_id] = []
            return []
        size = max(1, min(slots, len(refs)))
        start = _stable_index(conversation_id, len(refs))
        selected = [refs[(start + offset) % len(refs)].ref for offset in range(size)]
        self._assignments[conversation_id] = selected
        return list(selected)


def _parse_key_file_line(line: str) -> tuple[str, str, str | None] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    stripped = stripped.strip("` ")
    if "=" in stripped:
        name, value = stripped.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            if value:
                return (_secret_ref(name, value), "file_secret", value)
            return (name, "file_env", None)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", stripped):
        return (stripped, "file_env", None)
    return (_secret_ref("file", stripped), "file_secret", stripped)


def _secret_ref(name: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    label = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "file"
    return f"{label}:secret:{digest}"


def _parse_env_name(line: str) -> str | None:
    parsed = _parse_key_file_line(line)
    if parsed and parsed[1] == "file_env":
        return parsed[0]
    return None


def _stable_index(value: str, modulo: int) -> int:
    if modulo <= 0:
        return 0
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo
