"""Redaction of Claude's own on-disk session material, and the `.redacted` marker.

Claude writes transcripts, subagent transcripts, file-history backups, debug logs, todos and
shell snapshots under its config dir (`<session dir>/config`). These files can leave the pod
(a session archive), so once claude has stopped the wrapper rewrites every text file there
through the service redactor and then writes `<session dir>/.redacted`. A session dir without
a current marker may hold unredacted material: the marker is removed whenever claude starts
again in that dir, and a startup pass redacts every unmarked dir a previous process left.

Rules:
- Only regular files under the config dir; symlinks are never followed or rewritten.
- `.credentials.json` (claude's login) and `plugins/` (installed plugin code) are left alone.
- A file whose first bytes contain NUL, or that is not UTF-8, is binary and left alone.
- `.jsonl` files stream line by line (memory bounded by the longest line); other text files
  are read whole up to `WHOLE_FILE_MAX_BYTES`, line by line above it.
- A JSON document or line is redacted by string (keys and values), so escaping inside the JSON
  never hides a value and the result stays valid JSON; identifier and timestamp fields that
  claude's resume chain depends on are kept as they are. Other text is redacted as text.
- A file is replaced atomically (temp file in the same dir, then rename) and only when its
  content changed; the mode and the timestamps of the original are kept.
- Nothing logged here carries file content: counts and the session dir only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from . import __version__
from .redact import Redactor
from .timeutil import now_iso

log = logging.getLogger(__name__)

MARKER_FILE = ".redacted"
#: Bumped when the rules change, so dirs marked by an older pass are redacted again.
MARKER_FORMAT = 1
SKIP_FILES = frozenset({".credentials.json"})
SKIP_DIRS = frozenset({"plugins", ".git"})
SNIFF_BYTES = 8192
TMP_SUFFIX = ".redact-tmp"
WHOLE_FILE_MAX_BYTES = 32 * 1024 * 1024
#: Machine-generated identifiers and timestamps in claude's JSON: never a secret, and masking
#: a short listed value inside them would break the uuid chain a resume follows.
KEEP_KEYS = frozenset(
    {
        "uuid",
        "parentUuid",
        "leafUuid",
        "logicalParentUuid",
        "sessionId",
        "agentId",
        "requestId",
        "messageId",
        "promptId",
        "id",
        "tool_use_id",
        "toolUseID",
        "sourceToolUseID",
        "timestamp",
        "type",
        "role",
        "version",
    }
)


@dataclass
class RedactStats:
    files: int = 0
    rewritten: int = 0
    skipped: int = 0
    errors: int = 0


def _redact_value(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, dict):
        return {
            redactor.redact(k): v
            if k in KEEP_KEYS and isinstance(v, str)
            else _redact_value(v, redactor)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v, redactor) for v in value]
    return value


def redact_text(text: str, redactor: Redactor, *, indent: int | None = None) -> str:
    """`text` redacted: by JSON value when it parses as JSON, else as plain text."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            doc = json.loads(stripped)
        except ValueError:
            doc = None
        if doc is not None:
            out = _redact_value(doc, redactor)
            if out == doc:
                return text
            dumped = json.dumps(
                out, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":")
            )
            return text[: len(text) - len(text.lstrip())] + dumped + text[len(text.rstrip()) :]
    return redactor.redact(text)


def _is_binary(path: Path) -> bool:
    with path.open("rb") as fh:
        return b"\0" in fh.read(SNIFF_BYTES)


def _lines(fh: IO[bytes]) -> Iterator[str]:
    for raw in fh:
        yield raw.decode("utf-8")  # UnicodeDecodeError: not text, the caller skips the file


def _rewrite(path: Path, redactor: Redactor, st: os.stat_result) -> bool:
    """Write the redacted copy next to `path` and swap it in if it differs. Returns whether
    the file was replaced; raises UnicodeDecodeError for a file that is not UTF-8."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out, path.open("rb") as src:
            if path.suffix == ".jsonl" or st.st_size > WHOLE_FILE_MAX_BYTES:
                changed = False
                for line in _lines(src):
                    body = line.rstrip("\r\n")
                    new = redact_text(body, redactor) if body else body
                    changed = changed or new != body
                    out.write(new + line[len(body) :])
            else:
                text = src.read().decode("utf-8")
                new = redact_text(text, redactor, indent=2 if "\n  " in text else None)
                changed = new != text
                out.write(new)
            out.flush()
            os.fsync(out.fileno())
        if changed:
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, path)
            return True
        os.unlink(tmp)
        return False
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name not in SKIP_FILES and not name.endswith(TMP_SUFFIX):
                yield Path(dirpath) / name


def redact_tree(root: Path, redactor: Redactor) -> RedactStats:
    """Redact every text file under `root` in place (see the module rules)."""
    stats = RedactStats()
    if not root.is_dir():
        return stats
    for path in _files(root):
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue
            stats.files += 1
            if _is_binary(path):
                stats.skipped += 1
                continue
            if _rewrite(path, redactor, st):
                stats.rewritten += 1
        except UnicodeDecodeError:
            stats.skipped += 1
        except OSError as exc:
            stats.errors += 1
            log.warning("redact: %s under %s: %s", type(exc).__name__, root, exc.strerror)
    return stats


def marker_path(session_dir: Path) -> Path:
    return session_dir / MARKER_FILE


def is_marked(session_dir: Path) -> bool:
    """Whether `session_dir` carries a marker of the current format."""
    try:
        doc = json.loads(marker_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("format") == MARKER_FORMAT


def clear_marker(session_dir: Path) -> None:
    """Claude is about to write in `session_dir` again: its material is unredacted."""
    with contextlib.suppress(FileNotFoundError):
        marker_path(session_dir).unlink()


def _write_marker(session_dir: Path) -> None:
    doc = {"format": MARKER_FORMAT, "wrapper_version": __version__, "redacted_at": now_iso()}
    path = marker_path(session_dir)
    fd, tmp = tempfile.mkstemp(dir=session_dir, prefix=f"{MARKER_FILE}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def redact_session_dir(session_dir: Path, redactor: Redactor) -> RedactStats:
    """Redact the session's config dir, then mark the session dir. The marker is written only
    when every file was handled (a read or write error leaves the dir unmarked)."""
    stats = redact_tree(session_dir / "config", redactor)
    if stats.errors == 0 and session_dir.is_dir():
        _write_marker(session_dir)
    log.info(
        "redact: %s: %d files, %d rewritten, %d skipped, %d errors",
        session_dir.name,
        stats.files,
        stats.rewritten,
        stats.skipped,
        stats.errors,
    )
    return stats
