from __future__ import annotations

import os
import re
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.personal_wechat_bot.config.schema import ProviderConfig


# Default key-file name under the data dir, used when no api_key_file is set so
# the sidebar add-key flow works on a fresh install. Created lazily on first add.
_DEFAULT_KEY_FILE_NAME = "api_keys.local.md"


@dataclass(frozen=True)
class ApiKeyRef:
    ref: str
    source: str
    available: bool


def _mask_secret(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    # Never reveal the whole secret. Only expose a last-4 tail when at least as
    # many characters stay hidden (len >= 8); a shorter/mis-pasted value is
    # masked entirely so it can't be echoed in full to the UI or logs.
    if len(value) >= 8:
        return f"****{value[-4:]}"
    return "****"


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

    def available_refs(self) -> list[str]:
        """Refs whose secret value currently resolves, in pool order.

        Used by the client's per-request failover to enumerate keys it may try
        when the primary pick returns an auth/rate error.
        """
        return [item.ref for item in self.refs() if self.key_for_ref(item.ref)]

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

    def key_file_path(self) -> Path | None:
        if not self.provider.api_key_file:
            # No explicit key file configured: fall back to a default location
            # under the data dir so the sidebar add-key flow works out of the box
            # on a fresh install. The file is only created when a key is actually
            # added; reads tolerate its absence, so this is a no-op until used.
            return self.data_dir / _DEFAULT_KEY_FILE_NAME
        path = Path(self.provider.api_key_file)
        if not path.is_absolute():
            path = self.data_dir / path
        return path

    def _key_file_lines(self) -> list[str]:
        path = self.key_file_path()
        if path is None or not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return text.splitlines()

    def describe(self) -> list[dict[str, str | bool]]:
        """Return refs enriched with a masked preview, safe to expose to the UI.

        Raw key values are never included — only a ``****tail`` masked preview and
        the anonymized ref. For env-var keys the preview reflects the resolved
        environment value (if set), so the operator can tell a configured slot
        from an empty one without seeing the secret.
        """
        secret_values = self._file_secret_values()
        described: list[dict[str, str | bool]] = []
        for item in self.refs():
            if item.source == "file_secret":
                preview = _mask_secret(secret_values.get(item.ref, ""))
            else:
                preview = _mask_secret(os.environ.get(item.ref, ""))
            described.append(
                {
                    "ref": item.ref,
                    "source": item.source,
                    "available": item.available,
                    "preview": preview,
                }
            )
        return described

    def add_key(self, value: str, name: str | None = None) -> ApiKeyRef:
        """Append a literal secret key to the key file as ``NAME = value``.

        Returns the anonymized ref for the new key. Raises ValueError when no
        key file is configured or the value is empty/duplicate.

        The duplicate-check + append run under a cross-process lock so two
        concurrent add/remove operations (threaded HTTP server, multiple app
        instances) can't interleave their read-modify-write and lose a write or
        concatenate two entries onto one line.
        """
        value = value.strip()
        if not value:
            raise ValueError("api key value is empty")
        path = self.key_file_path()
        if path is None:
            raise ValueError("no api_key_file configured for this provider")
        with self._file_lock(path):
            if value in self._file_secret_values().values():
                raise ValueError("api key already present in pool")
            name = (name or "").strip() or self._next_key_name()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid key name")
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{prefix}{name} = {value}\n")
            return ApiKeyRef(ref=_secret_ref(name, value), source="file_secret", available=True)

    def remove_key(self, ref: str) -> bool:
        """Remove the key file line whose parsed ref matches ``ref``.

        Only file-backed keys can be removed (env-var pool entries live in
        config, not the key file). Returns True when a line was removed. Runs
        under the same cross-process lock as :meth:`add_key`.
        """
        path = self.key_file_path()
        if path is None or not path.exists():
            return False
        with self._file_lock(path):
            if not path.exists():
                return False
            lines = path.read_text(encoding="utf-8").splitlines()
            kept: list[str] = []
            removed = False
            for line in lines:
                parsed = _parse_key_file_line(line)
                if parsed is not None and parsed[0] == ref and not removed:
                    removed = True
                    continue
                kept.append(line)
            if not removed:
                return False
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            return True

    def _file_lock(self, path: Path):
        """Cross-process lock guarding read-modify-write of the key file."""
        from app.personal_wechat_bot.runtime.process_lock import blocking_process_lock

        return blocking_process_lock(
            path.with_name(path.name + ".lock"),
            label="api_key_pool",
            stale_after_seconds=30.0,
            wait_timeout_seconds=15.0,
        )

    def _next_key_name(self) -> str:
        """Derive the next ``PREFIX_NN`` name from existing file entries."""
        prefix = "DEEPSEEK_KEY"
        max_index = 0
        pattern = re.compile(r"^([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*)_(\d+)$")
        for line in self._key_file_lines():
            stripped = line.strip().strip("` ")
            name = stripped.split("=", 1)[0].strip() if "=" in stripped else stripped
            match = pattern.match(name)
            if match:
                prefix = match.group(1)
                max_index = max(max_index, int(match.group(2)))
        return f"{prefix}_{max_index + 1:02d}"


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
