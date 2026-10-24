"""Read-only access to a session workdir: confinement (part `wrapper-path-confinement`).

`resolve_in_workdir` is the one path resolver of the file API (and of the terminal shell cwd).
The listing and preview helpers below are blocking; callers run them in a worker thread.
"""

from __future__ import annotations

import datetime
import errno
import mimetypes
import os
import stat
from pathlib import Path
from typing import Any

#: Bytes of a file inspected for a NUL byte when classifying it as binary.
BINARY_SNIFF_BYTES = 8192


class FileError(Exception):
    """A file API failure: `code` is the API error code, `message` is for humans."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_in_workdir(workdir: str, rel: str) -> Path:
    """The real path of `rel` inside `workdir`; `ValueError` when it is absolute, holds a NUL
    byte, or resolves (through `..` or symlinks) outside the workdir's real path."""
    if "\x00" in workdir or "\x00" in rel:
        raise ValueError("path contains a NUL byte")
    if os.path.isabs(rel):
        raise ValueError("path must be relative to the workdir")
    root = Path(os.path.realpath(workdir))
    resolved = Path(os.path.realpath(root / rel))
    if resolved != root and not resolved.is_relative_to(root):
        raise ValueError("path escapes the workdir")
    return resolved


def _iso(timestamp: float) -> str:
    return (
        datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _printable(name: str) -> str:
    """JSON-safe form of a file name (undecodable bytes become U+FFFD)."""
    return name.encode("utf-8", "replace").decode("utf-8")


def _resolve(workdir: str | None, rel: str) -> tuple[Path, Path]:
    """(root, resolved) for a request; `FileError` for an escape or a vanished workdir."""
    if not workdir or not os.path.isdir(workdir):
        raise FileError("not_found", "the session workdir does not exist")
    try:
        resolved = resolve_in_workdir(workdir, rel)
    except ValueError as exc:
        raise FileError("invalid_path", str(exc)) from exc
    return Path(os.path.realpath(workdir)), resolved


def _open_nofollow(path: Path) -> int:
    """Open `path` without following a final symlink and without blocking on a FIFO."""
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise FileError("not_found", "no such file or directory") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FileError("invalid_path", "path is a symlink that cannot be followed") from exc
        raise FileError("invalid_path", f"path is not readable: {exc.strerror}") from exc


def _entry_type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def list_directory(workdir: str | None, rel: str, offset: int, max_entries: int) -> dict[str, Any]:
    """One page of a workdir directory: directories first, then the rest, each by name bytes."""
    root, resolved = _resolve(workdir, rel)
    fd = _open_nofollow(resolved)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise FileError("not_a_directory", "path is not a directory")
        rows: list[tuple[bool, bytes, dict[str, Any]]] = []
        with os.scandir(fd) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue  # removed between the listing and the stat
                kind = _entry_type(st.st_mode)
                rows.append(
                    (
                        kind != "dir",
                        os.fsencode(entry.name),
                        {
                            "name": _printable(entry.name),
                            "type": kind,
                            "size": st.st_size if kind == "file" else None,
                            "mtime": _iso(st.st_mtime),
                        },
                    )
                )
    except OSError as exc:
        raise FileError("not_found", f"cannot list directory: {exc.strerror}") from exc
    finally:
        os.close(fd)
    rows.sort(key=lambda row: (row[0], row[1]))
    page = [row[2] for row in rows[offset : offset + max_entries]]
    next_offset = offset + max_entries if offset + max_entries < len(rows) else None
    rel_path = "" if resolved == root else resolved.relative_to(root).as_posix()
    return {"root": str(root), "path": rel_path, "entries": page, "next_offset": next_offset}


def read_preview(workdir: str | None, rel: str, max_bytes: int) -> dict[str, Any]:
    """Metadata and, for a small text file, the decoded content of one regular file.

    The file is opened once (`O_NOFOLLOW | O_NONBLOCK`) after the confinement check and
    everything else comes from that descriptor, so a path swapped after the check cannot hang
    or redirect the read.
    """
    root, resolved = _resolve(workdir, rel)
    fd = _open_nofollow(resolved)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FileError("not_a_file", "path is not a regular file")
        out: dict[str, Any] = {
            "path": "" if resolved == root else resolved.relative_to(root).as_posix(),
            "size": st.st_size,
            "mtime": _iso(st.st_mtime),
            "mime": mimetypes.guess_type(resolved.name)[0],
            "kind": "too_large",
            "content": None,
        }
        if st.st_size > max_bytes:
            return out
        chunks: list[bytes] = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise FileError("not_found", f"cannot read file: {exc.strerror}") from exc
    finally:
        os.close(fd)
    data = b"".join(chunks)
    try:
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "NUL byte")
        out["content"] = data.decode("utf-8")
        out["kind"] = "text"
    except UnicodeDecodeError:
        out["kind"] = "binary"
    return out
