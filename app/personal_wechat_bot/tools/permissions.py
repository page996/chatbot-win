from pathlib import Path


def ensure_within(path: str | Path, root: str | Path) -> Path:
    candidate = Path(path).resolve()
    root_path = Path(root).resolve()
    if root_path not in candidate.parents and candidate != root_path:
        raise PermissionError(f"path outside allowed root: {candidate}")
    return candidate


def resolve_allowed_roots(data_dir: str | Path, roots: list[str]) -> list[Path]:
    base = Path(data_dir).resolve()
    resolved: list[Path] = []
    for item in roots:
        root = Path(item)
        if not root.is_absolute():
            root = base / root
        resolved.append(root.resolve())
    return resolved


def validate_readable_file(
    path: str | Path,
    allowed_roots: list[Path],
    allowed_extensions: list[str],
    max_bytes: int,
) -> Path:
    candidate = _resolve_candidate(path, allowed_roots)
    if not _is_within_any(candidate, allowed_roots):
        raise PermissionError(f"path outside allowed roots: {candidate}")
    if not candidate.exists():
        raise FileNotFoundError(f"file not found: {candidate}")
    if not candidate.is_file():
        raise PermissionError(f"path is not a file: {candidate}")
    allowed = {ext.lower() for ext in allowed_extensions}
    if allowed and candidate.suffix.lower() not in allowed:
        raise PermissionError(f"file extension not allowed: {candidate.suffix}")
    size = candidate.stat().st_size
    if size > max_bytes:
        raise PermissionError(f"file too large: {size} > {max_bytes}")
    return candidate


def _resolve_candidate(path: str | Path, allowed_roots: list[Path]) -> Path:
    raw = Path(path)
    if raw.is_absolute():
        return raw.resolve()
    for root in allowed_roots:
        candidate = (root / raw).resolve()
        if candidate.exists():
            return candidate
    if not allowed_roots:
        return raw.resolve()
    return (allowed_roots[0] / raw).resolve()


def _is_within_any(candidate: Path, roots: list[Path]) -> bool:
    return any(root == candidate or root in candidate.parents for root in roots)
