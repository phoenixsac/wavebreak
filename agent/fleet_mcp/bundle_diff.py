"""Summarised diff between two firmware bundle directories (root-cause hint for verdicts)."""

from __future__ import annotations

import difflib
from pathlib import Path

from .errors import PreconditionError


def _bundle_dir(bundles_dir: str | Path, version: str) -> Path:
    if not version or "/" in version or "\\" in version or version in {".", ".."}:
        raise PreconditionError("BAD_ARGUMENT", f"invalid version name {version!r}")
    path = Path(bundles_dir) / version
    if not path.is_dir():
        raise PreconditionError("UNKNOWN_VERSION", f"no bundle directory for {version} under {bundles_dir}")
    return path


def _files(root: Path) -> dict[str, Path]:
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _text(path: Path) -> list[str] | None:
    """File lines, or None if unreadable or binary."""
    try:
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:
            return None
        return raw.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _diff_lines(old: list[str], new: list[str]) -> list[str]:
    """Only `@@` headers and +/- lines of the unified diff."""
    out = []
    for line in difflib.unified_diff(old, new, lineterm="", n=0):
        if line.startswith(("+++", "---")):
            continue
        out.append(line)
    return out


def _entry(
    path: str, status: str, old: list[str] | None, new: list[str] | None, binary: bool, cap: int
) -> dict:
    if binary:
        return {"path": path, "status": status, "lines_added": 0, "lines_removed": 0, "diff": ""}
    lines = _diff_lines(old or [], new or [])
    added = sum(1 for ln in lines if ln.startswith("+"))
    removed = sum(1 for ln in lines if ln.startswith("-"))
    if len(lines) > cap:
        lines = [*lines[:cap], f"... ({len(lines) - cap} more lines)"]
    return {
        "path": path,
        "status": status,
        "lines_added": added,
        "lines_removed": removed,
        "diff": "\n".join(lines),
    }


def bundle_diff(
    bundles_dir: str | Path,
    from_version: str,
    to_version: str,
    *,
    max_files: int = 20,
    max_lines_per_file: int = 40,
) -> dict:
    """Compare `<bundles_dir>/<from>` with `<to>`; raises UNKNOWN_VERSION if either is missing."""
    old_files = _files(_bundle_dir(bundles_dir, from_version))
    new_files = _files(_bundle_dir(bundles_dir, to_version))
    entries = []
    for rel in sorted(old_files.keys() | new_files.keys()):
        old_path, new_path = old_files.get(rel), new_files.get(rel)
        if old_path is None:
            status = "added"
        elif new_path is None:
            status = "removed"
        elif old_path.read_bytes() == new_path.read_bytes():
            continue
        else:
            status = "changed"
        old = _text(old_path) if old_path else []
        new = _text(new_path) if new_path else []
        entries.append(_entry(rel, status, old, new, old is None or new is None, max_lines_per_file))
    counts = {s: sum(1 for e in entries if e["status"] == s) for s in ("changed", "added", "removed")}
    result = {
        "from": from_version,
        "to": to_version,
        "summary": f"{counts['changed']} changed, {counts['added']} added, {counts['removed']} removed files",
        "files": entries[:max_files],
    }
    if len(entries) > max_files:
        result["omitted_files"] = len(entries) - max_files
    return result
