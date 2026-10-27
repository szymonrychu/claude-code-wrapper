"""Session file API: listing, content, confinement and auth over a stub manager."""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from claude_code_wrapper import files as files_mod
from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.files import resolve_in_workdir
from claude_code_wrapper.manager import SessionNotFound
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-0123456789abcdef"
H = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_000000000001"
GONE = "ses_000000000002"
MIB = 1048576


class StubManager:
    def __init__(self, workdir: Path, gone: Path) -> None:
        self.started = True
        self.redactor = Redactor()
        self.workdir = workdir
        self.gone = gone

    def get(self, sid: str) -> dict[str, Any]:
        if sid == SID:
            return {"session_id": sid, "workdir": str(self.workdir)}
        if sid == GONE:
            return {"session_id": sid, "workdir": str(self.gone)}
        raise SessionNotFound(sid)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    w.mkdir()
    return w


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    o = tmp_path / "outside"
    o.mkdir()
    (o / "secret.txt").write_text("TOP-SECRET")
    return o


def make_client(work: Path, tmp_path: Path, **limits: int) -> TestClient:
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}, "limits": limits})
    return TestClient(create_app(StubManager(work, tmp_path / "missing"), cfg), headers=H)


@pytest.fixture
def client(work: Path, tmp_path: Path) -> TestClient:
    return make_client(work, tmp_path)


def ls(client: TestClient, path: str | None = None, offset: int | None = None, sid: str = SID):
    params: dict[str, Any] = {}
    if path is not None:
        params["path"] = path
    if offset is not None:
        params["offset"] = offset
    return client.get(f"/v1/sessions/{sid}/files", params=params)


def cat(client: TestClient, path: str, sid: str = SID):
    return client.get(f"/v1/sessions/{sid}/files/content", params={"path": path})


def dl(client: TestClient, path: str, sid: str = SID):
    return client.get(f"/v1/sessions/{sid}/files/download", params={"path": path})


def put(client: TestClient, path: str, content: Any, expected: Any = None, sid: str = SID):
    return client.put(
        f"/v1/sessions/{sid}/files/content",
        params={"path": path},
        json={"content": content, "expected": expected},
    )


def stamp(client: TestClient, path: str) -> dict[str, Any]:
    body = cat(client, path).json()
    return {"mtime": body["mtime"], "size": body["size"]}


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def code(r) -> str:
    return r.json()["error"]["code"]


def test_list_root_dirs_first_includes_dotfiles(client: TestClient, work: Path) -> None:
    (work / "zdir").mkdir()
    (work / ".git").mkdir()
    (work / "b.txt").write_text("hello")
    (work / ".env").write_text("")
    (work / "A.txt").write_text("x")
    (work / "link").symlink_to("b.txt")
    r = ls(client)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == SID
    assert body["root"] == os.path.realpath(work)
    assert body["path"] == ""
    assert body["next_offset"] is None
    names = [(e["name"], e["type"]) for e in body["entries"]]
    assert names == [
        (".git", "dir"),
        ("zdir", "dir"),
        (".env", "file"),
        ("A.txt", "file"),
        ("b.txt", "file"),
        ("link", "symlink"),
    ]
    by = {e["name"]: e for e in body["entries"]}
    assert by["b.txt"]["size"] == 5
    assert by["zdir"]["size"] is None
    for e in body["entries"]:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", e["mtime"])


def test_list_subdirectory_and_normalised_path(client: TestClient, work: Path) -> None:
    (work / "a" / "b").mkdir(parents=True)
    (work / "a" / "b" / "f.txt").write_text("x")
    r = ls(client, "a/./b/")
    assert r.status_code == 200
    assert r.json()["path"] == "a/b"
    assert [e["name"] for e in r.json()["entries"]] == ["f.txt"]


def test_list_pages_with_next_offset(work: Path, tmp_path: Path) -> None:
    client = make_client(work, tmp_path, files_max_entries=2)
    for n in ("a", "b", "c", "d", "e"):
        (work / n).write_text(n)
    seen: list[str] = []
    offset: int | None = 0
    pages = 0
    while offset is not None:
        body = ls(client, offset=offset).json()
        assert len(body["entries"]) <= 2
        seen += [e["name"] for e in body["entries"]]
        offset = body["next_offset"]
        pages += 1
    assert seen == ["a", "b", "c", "d", "e"]
    assert pages == 3
    assert ls(client, offset=4).json()["next_offset"] is None
    assert ls(client, offset=2).json()["next_offset"] == 4


def test_escapes_refused(client: TestClient, work: Path, outside: Path) -> None:
    (work / "out").symlink_to(outside)
    (work / "leak.txt").symlink_to(outside / "secret.txt")
    (work / "sub").mkdir()
    for bad in ("..", "../outside", "sub/../..", str(outside), "/etc", "a\x00b", "out", "out/x"):
        r = ls(client, bad)
        assert r.status_code == 400 and code(r) == "invalid_path", bad
    for bad in ("../outside/secret.txt", str(outside / "secret.txt"), "leak.txt", "out/secret.txt"):
        r = cat(client, bad)
        assert r.status_code == 400 and code(r) == "invalid_path", bad
        assert "TOP-SECRET" not in r.text
    with pytest.raises(ValueError):
        resolve_in_workdir(str(work), "../outside")


def test_symlink_inside_workdir_followed(client: TestClient, work: Path) -> None:
    (work / "real.txt").write_text("inside")
    (work / "link.txt").symlink_to("real.txt")
    r = cat(client, "link.txt")
    assert r.status_code == 200 and r.json()["content"] == "inside"


def test_content_kinds(work: Path, tmp_path: Path) -> None:
    client = make_client(work, tmp_path)
    (work / "a.txt").write_text("h\u00e9llo\n")
    (work / "nul.bin").write_bytes(b"ab\x00cd")
    (work / "bad.txt").write_bytes(b"\xff\xfe\xfa")
    (work / "late-nul").write_bytes(b"a" * 9000 + b"\x00")
    (work / "empty").write_bytes(b"")
    (work / "big.txt").write_bytes(b"a" * (MIB + 1))
    (work / "edge.txt").write_bytes(b"a" * MIB)

    body = cat(client, "a.txt").json()
    assert body["kind"] == "text" and body["content"] == "h\u00e9llo\n"
    assert body["session_id"] == SID and body["path"] == "a.txt"
    assert body["size"] == len("h\u00e9llo\n".encode()) and body["mime"] == "text/plain"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", body["mtime"])
    for name in ("nul.bin", "bad.txt"):
        body = cat(client, name).json()
        assert body["kind"] == "binary" and body["content"] is None, name
    (work / "blob").write_bytes(b"\x00")
    assert cat(client, "blob").json()["mime"] is None
    # a NUL past the first 8 KiB is not looked for, but the bytes are valid UTF-8 text
    assert cat(client, "late-nul").json()["kind"] == "text"
    assert cat(client, "empty").json()["content"] == ""
    assert cat(client, "edge.txt").json()["kind"] == "text"
    body = cat(client, "big.txt").json()
    assert body["kind"] == "too_large" and body["content"] is None and body["size"] == MIB + 1


def test_content_cap_is_configurable(work: Path, tmp_path: Path) -> None:
    client = make_client(work, tmp_path, file_preview_max_bytes=4)
    (work / "f").write_text("12345")
    assert cat(client, "f").json()["kind"] == "too_large"
    (work / "g").write_text("1234")
    assert cat(client, "g").json()["kind"] == "text"


def test_fifo_and_type_mismatch_refused(client: TestClient, work: Path) -> None:
    os.mkfifo(work / "pipe")
    (work / "d").mkdir()
    (work / "f.txt").write_text("x")
    r = cat(client, "pipe")  # would hang forever if opened blocking or read
    assert r.status_code == 400 and code(r) == "not_a_file"
    r = cat(client, "d")
    assert r.status_code == 400 and code(r) == "not_a_file"
    r = cat(client, "")
    assert r.status_code == 400 and code(r) == "not_a_file"
    r = ls(client, "f.txt")
    assert r.status_code == 400 and code(r) == "not_a_directory"
    r = ls(client, "pipe")
    assert r.status_code == 400 and code(r) == "not_a_directory"
    listed = {e["name"]: e["type"] for e in ls(client).json()["entries"]}
    assert listed["pipe"] == "other"


def test_content_open_nofollow_and_fstat(
    client: TestClient, work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opens: list[int] = []
    real_open = os.open

    def spy(path, flags, *a, **k):
        opens.append(flags)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(files_mod.os, "open", spy)
    (work / "ok.txt").write_text("fine")
    assert cat(client, "ok.txt").status_code == 200
    want = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    assert opens and all(f & want == want for f in opens)

    real_resolve = files_mod.resolve_in_workdir

    def swap_to(kind: str):
        def resolve(workdir, rel):
            resolved = real_resolve(workdir, rel)
            resolved.unlink()
            if kind == "fifo":
                os.mkfifo(resolved)
            elif kind == "symlink":
                resolved.symlink_to(outside / "secret.txt")
            elif kind == "device":
                resolved.symlink_to("/dev/zero")
            return resolved

        return resolve

    for kind in ("fifo", "symlink", "device"):
        (work / "t.txt").write_text("x")
        monkeypatch.setattr(files_mod, "resolve_in_workdir", swap_to(kind))
        r = cat(client, "t.txt")
        assert r.status_code == 400, kind
        assert code(r) in ("not_a_file", "invalid_path"), kind
        assert "TOP-SECRET" not in r.text
        (work / "t.txt").unlink()
    # a character device reached directly (not via a symlink) is refused by fstat
    monkeypatch.setattr(files_mod, "resolve_in_workdir", real_resolve)
    monkeypatch.setattr(files_mod, "resolve_in_workdir", lambda workdir, rel: Path("/dev/zero"))
    r = cat(client, "anything")
    assert r.status_code == 400 and code(r) == "not_a_file"


def test_auth_and_not_found(client: TestClient, work: Path) -> None:
    (work / "f.txt").write_text("x")
    bare = TestClient(client.app)
    assert bare.get(f"/v1/sessions/{SID}/files").status_code == 401
    assert (
        bare.get(f"/v1/sessions/{SID}/files/content", params={"path": "f.txt"}).status_code == 401
    )
    for r in (
        ls(client, sid="ses_ffffffffffff"),
        cat(client, "f.txt", sid="ses_ffffffffffff"),
        ls(client, "nope"),
        ls(client, "nope/deeper"),
        cat(client, "nope.txt"),
        cat(client, "f.txt/child"),
        ls(client, sid=GONE),
        cat(client, "f.txt", sid=GONE),
    ):
        assert r.status_code == 404 and code(r) == "not_found"


def test_bad_offset_is_422(client: TestClient) -> None:
    assert ls(client, offset=-1).status_code == 422


def test_download_bytes_exact(client: TestClient, work: Path) -> None:
    blobs = {
        "a.txt": "h\u00e9llo\n".encode(),
        "nul.bin": b"ab\x00cd\xff\xfe",
        "bad.txt": b"\xff\xfe\xfa",
        "big.bin": bytes(range(256)) * (3 * MIB // 256) + b"!",
    }
    assert len(blobs["big.bin"]) == 3 * MIB + 1
    for name, data in blobs.items():
        (work / name).write_bytes(data)
        r = dl(client, name)
        assert r.status_code == 200, name
        assert r.content == data, name
        assert r.headers["content-length"] == str(len(data)), name
    (work / "empty").write_bytes(b"")
    r = dl(client, "empty")
    assert r.status_code == 200 and r.content == b"" and r.headers["content-length"] == "0"


def test_download_ignores_preview_cap(work: Path, tmp_path: Path) -> None:
    client = make_client(work, tmp_path, file_preview_max_bytes=4)
    (work / "ten").write_bytes(b"0123456789")
    assert cat(client, "ten").json()["kind"] == "too_large"
    r = dl(client, "ten")
    assert r.status_code == 200 and r.content == b"0123456789"


def test_download_content_type_and_safety_headers(client: TestClient, work: Path) -> None:
    (work / "m.py").write_text("print(1)\n")
    (work / "blob").write_bytes(b"\x00")
    r = dl(client, "m.py")
    assert r.headers["content-type"] == "text/x-python"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    r = dl(client, "blob")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_download_content_disposition(client: TestClient, work: Path) -> None:
    name = 'caf\u00e9 "q"\\x.txt'
    (work / name).write_text("x")
    r = dl(client, name)
    assert r.status_code == 200
    assert r.headers["content-disposition"] == (
        "attachment; filename=\"caf_ _q__x.txt\"; filename*=UTF-8''caf%C3%A9%20%22q%22%5Cx.txt"
    )
    # a plain name: fallback and extended value agree
    (work / "plain-1_2.txt").write_text("x")
    assert dl(client, "plain-1_2.txt").headers["content-disposition"] == (
        "attachment; filename=\"plain-1_2.txt\"; filename*=UTF-8''plain-1_2.txt"
    )
    # a symlink downloads under the link name, not the target name
    (work / "real-target.txt").write_text("target")
    (work / "the-link.txt").symlink_to("real-target.txt")
    r = dl(client, "the-link.txt")
    assert r.content == b"target"
    assert 'filename="the-link.txt"' in r.headers["content-disposition"]
    assert "real-target" not in r.headers["content-disposition"]
    # a nested, unnormalised path uses its last segment
    (work / "a" / "b").mkdir(parents=True)
    (work / "a" / "b" / "f.txt").write_text("x")
    r = dl(client, "a/./b/../b/f.txt")
    assert r.status_code == 200
    assert 'filename="f.txt"' in r.headers["content-disposition"]
    # undecodable name bytes become what the listing's name shows ("?")
    assert files_mod.content_disposition("bad\udcff.txt") == (
        "attachment; filename=\"bad?.txt\"; filename*=UTF-8''bad%3F.txt"
    )
    assert files_mod._printable("bad\udcff.txt") == "bad?.txt"


def test_download_guards(client: TestClient, work: Path, outside: Path) -> None:
    (work / "out").symlink_to(outside)
    (work / "leak.txt").symlink_to(outside / "secret.txt")
    (work / "loop").symlink_to("loop")
    (work / "sub").mkdir()
    os.mkfifo(work / "pipe")
    for bad in (
        "..",
        "../outside/secret.txt",
        str(outside / "secret.txt"),
        "a\x00b",
        "leak.txt",
        "out/secret.txt",
        "sub/../../outside/secret.txt",
        "loop",
    ):
        r = dl(client, bad)
        assert r.status_code == 400 and code(r) == "invalid_path", bad
        assert "TOP-SECRET" not in r.text
    for bad in ("sub", "pipe", ""):  # a directory, a FIFO and the workdir root
        r = dl(client, bad)
        assert r.status_code == 400 and code(r) == "not_a_file", bad
    for r in (
        dl(client, "nope.txt"),
        dl(client, "nope/deeper"),
        dl(client, "x", sid="ses_ffffffffffff"),
        dl(client, "x", sid=GONE),
    ):
        assert r.status_code == 404 and code(r) == "not_found"
    (work / "f.txt").write_text("x")
    bare = TestClient(client.app)
    r = bare.get(f"/v1/sessions/{SID}/files/download", params={"path": "f.txt"})
    assert r.status_code == 401 and code(r) == "unauthorized"


def test_download_open_nofollow_and_fstat(
    client: TestClient, work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opens: list[int] = []
    real_open = os.open

    def spy(path, flags, *a, **k):
        opens.append(flags)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(files_mod.os, "open", spy)
    (work / "ok.txt").write_text("fine")
    (work / "link.txt").symlink_to("ok.txt")
    assert dl(client, "ok.txt").content == b"fine"
    # a symlink inside the workdir to a regular file downloads the target bytes
    assert dl(client, "link.txt").content == b"fine"
    want = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    assert len(opens) == 2 and all(f & want == want for f in opens)

    real_resolve = files_mod.resolve_in_workdir

    def swap_to(kind: str):
        def resolve(workdir, rel):
            resolved = real_resolve(workdir, rel)
            resolved.unlink()
            if kind == "fifo":
                os.mkfifo(resolved)
            elif kind == "symlink":
                resolved.symlink_to(outside / "secret.txt")
            elif kind == "device":
                resolved.symlink_to("/dev/zero")
            return resolved

        return resolve

    for kind in ("fifo", "symlink", "device"):
        (work / "t.txt").write_text("x")
        monkeypatch.setattr(files_mod, "resolve_in_workdir", swap_to(kind))
        r = dl(client, "t.txt")
        assert r.status_code == 400, kind
        assert code(r) in ("not_a_file", "invalid_path"), kind
        assert "TOP-SECRET" not in r.text
        (work / "t.txt").unlink()
    # a character device reached directly (not via a symlink) is refused by fstat
    monkeypatch.setattr(files_mod, "resolve_in_workdir", lambda workdir, rel: Path("/dev/zero"))
    r = dl(client, "anything")
    assert r.status_code == 400 and code(r) == "not_a_file"


def test_download_closes_descriptor(client: TestClient, work: Path) -> None:
    (work / "big.bin").write_bytes(b"z" * (3 * MIB))
    (work / "d").mkdir()
    before = open_fds()
    assert len(dl(client, "big.bin").content) == 3 * MIB
    assert open_fds() == before
    assert dl(client, "d").status_code == 400
    assert dl(client, "nope").status_code == 404
    assert open_fds() == before
    # a client that goes away mid-stream
    with client.stream(
        "GET", f"/v1/sessions/{SID}/files/download", params={"path": "big.bin"}
    ) as r:
        assert next(r.iter_bytes())
    assert open_fds() == before
    # the helper's own handle: close is idempotent and ends the reads
    handle = files_mod.open_download(str(work), "big.bin")
    assert handle.size == 3 * MIB and handle.name == "big.bin"
    assert open_fds() == before + 1
    assert len(handle.read_chunk()) == MIB
    handle.close()
    handle.close()
    assert open_fds() == before
    assert handle.read_chunk() == b""


def test_list_symlink_target_type(client: TestClient, work: Path, outside: Path) -> None:
    (work / "f.txt").write_text("x")
    (work / "d").mkdir()
    os.mkfifo(work / "pipe")
    (work / "to-file").symlink_to("f.txt")
    (work / "to-dir").symlink_to("d")
    (work / "to-fifo").symlink_to("pipe")
    (work / "dangling").symlink_to("missing")
    (work / "loop").symlink_to("loop")
    (work / "out-file").symlink_to(outside / "secret.txt")
    (work / "out-dir").symlink_to(outside)
    (work / "d" / "up").symlink_to("../f.txt")
    by = {e["name"]: e for e in ls(client).json()["entries"]}
    assert {n: by[n]["target_type"] for n in by} == {
        "d": None,
        "f.txt": None,
        "pipe": None,
        "to-file": "file",
        "to-dir": "dir",
        "to-fifo": "other",
        "dangling": None,
        "loop": None,
        "out-file": None,
        "out-dir": None,
    }
    assert all(e["type"] == "symlink" for n, e in by.items() if n.startswith(("to-", "out-")))
    nested = ls(client, "d").json()["entries"]
    assert [(e["name"], e["target_type"]) for e in nested] == [("up", "file")]


def test_write_replaces_content(client: TestClient, work: Path) -> None:
    target = work / "sub" / "f.txt"
    target.parent.mkdir()
    target.write_text("old")
    for content in ("h\u00e9llo w\u00f6rld \u4e2d\u6587\n", "a\r\nb\r\nc", "no newline", ""):
        r = put(client, "sub/./f.txt", content)
        assert r.status_code == 200, content
        assert target.read_bytes() == content.encode("utf-8"), content
        body = r.json()
        assert set(body) == {"session_id", "path", "size", "mtime"}
        assert body["session_id"] == SID and body["path"] == "sub/f.txt"
        read = cat(client, "sub/f.txt").json()
        assert read["content"] == content
        assert (body["size"], body["mtime"]) == (read["size"], read["mtime"])
        assert body["size"] == len(content.encode("utf-8"))
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", body["mtime"])


def test_write_optimistic_concurrency(client: TestClient, work: Path) -> None:
    target = work / "f.txt"
    target.write_text("one")
    loaded = stamp(client, "f.txt")
    r = put(client, "f.txt", "two", expected=loaded)
    assert r.status_code == 200 and target.read_text() == "two"
    saved = {"mtime": r.json()["mtime"], "size": r.json()["size"]}
    assert saved["size"] == 3
    # the answer of a save is the baseline of the next one
    assert put(client, "f.txt", "three", expected=saved).status_code == 200

    current = stamp(client, "f.txt")
    for stale in (
        {"mtime": "2001-01-01T00:00:00.000Z", "size": current["size"]},
        {"mtime": current["mtime"], "size": current["size"] + 1},
        {"mtime": "2001-01-01T00:00:00.000Z", "size": current["size"] + 1},
    ):
        r = put(client, "f.txt", "lost", expected=stale)
        assert r.status_code == 409 and code(r) == "file_changed", stale
        assert target.read_text() == "three"
        assert stamp(client, "f.txt") == current
    # expected null overwrites unconditionally
    assert put(client, "f.txt", "forced", expected=None).status_code == 200
    assert target.read_text() == "forced"


def test_write_size_cap(work: Path, tmp_path: Path) -> None:
    client = make_client(work, tmp_path, file_preview_max_bytes=8)
    target = work / "f.txt"
    target.write_text("keep")
    # 5 characters but 10 UTF-8 bytes: the cap counts bytes
    r = put(client, "f.txt", "\u00e9" * 5)
    assert r.status_code == 413 and code(r) == "too_large"
    assert put(client, "f.txt", "123456789").status_code == 413
    assert target.read_text() == "keep"
    assert put(client, "f.txt", "\u00e9" * 4).status_code == 200
    assert target.read_bytes() == ("\u00e9" * 4).encode()
    assert put(client, "f.txt", "12345678").status_code == 200
    assert target.read_text() == "12345678"
    # an over-cap body never touches the disk, not even a missing path
    r = put(client, "missing.txt", "x" * 9)
    assert r.status_code == 413 and not (work / "missing.txt").exists()


def test_write_guards(client: TestClient, work: Path, outside: Path) -> None:
    secret = outside / "secret.txt"
    (work / "out").symlink_to(outside)
    (work / "leak.txt").symlink_to(secret)
    (work / "loop").symlink_to("loop")
    (work / "sub").mkdir()
    os.mkfifo(work / "pipe")
    for bad in (
        "..",
        "../outside/secret.txt",
        str(secret),
        "a\x00b",
        "leak.txt",
        "out/secret.txt",
        "sub/../../outside/secret.txt",
        "loop",
    ):
        r = put(client, bad, "PWNED")
        assert r.status_code == 400 and code(r) == "invalid_path", bad
        assert secret.read_text() == "TOP-SECRET"
    for bad in ("sub", "pipe", ""):  # a directory, a FIFO and the workdir itself
        r = put(client, bad, "PWNED")
        assert r.status_code == 400 and code(r) == "not_a_file", bad
    before = sorted(os.listdir(work))
    for r in (
        put(client, "nope.txt", "x"),
        put(client, "nope/deeper.txt", "x"),
        put(client, "x", "x", sid="ses_ffffffffffff"),
        put(client, "x", "x", sid=GONE),
    ):
        assert r.status_code == 404 and code(r) == "not_found"
    assert sorted(os.listdir(work)) == before  # nothing created
    assert not (work / "nope").exists()

    (work / "f.txt").write_text("keep")
    url = f"/v1/sessions/{SID}/files/content"
    bare = TestClient(client.app)
    r = bare.put(url, params={"path": "f.txt"}, json={"content": "x", "expected": None})
    assert r.status_code == 401 and code(r) == "unauthorized"
    for body in (
        {"expected": None},
        {"content": "x"},
        {"content": "x", "expected": None, "extra": 1},
        {"content": 5, "expected": None},
        {"content": None, "expected": None},
        {"content": "x", "expected": {"mtime": "t"}},
        {"content": "x", "expected": {"mtime": "t", "size": -1}},
        {"content": "x", "expected": {"mtime": "t", "size": 1, "extra": 1}},
    ):
        r = client.put(url, params={"path": "f.txt"}, json=body)
        assert r.status_code == 422 and code(r) == "invalid_request", body
    # a lone surrogate escape is valid JSON but not text a file can hold
    r = client.put(
        url,
        params={"path": "f.txt"},
        content=b'{"content":"lone \\ud800 surrogate","expected":null}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422 and code(r) == "invalid_request"
    assert (work / "f.txt").read_text() == "keep"


def test_write_open_nofollow_and_fstat(
    client: TestClient, work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opens: list[int] = []
    real_open = os.open

    def spy(path, flags, *a, **k):
        opens.append(flags)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(files_mod.os, "open", spy)
    (work / "real.txt").write_text("before")
    (work / "link.txt").symlink_to("real.txt")
    assert put(client, "real.txt", "direct").status_code == 200
    assert opens and all(f & os.O_NOFOLLOW for f in opens)
    # a symlink inside the workdir to a regular file writes the target and stays a link
    r = put(client, "link.txt", "through the link")
    assert r.status_code == 200 and r.json()["path"] == "real.txt"
    assert (work / "link.txt").is_symlink() and os.readlink(work / "link.txt") == "real.txt"
    assert (work / "real.txt").read_text() == "through the link"
    assert all(f & os.O_NOFOLLOW for f in opens)
    assert any(f & os.O_DIRECTORY for f in opens)  # the parent directory is opened by descriptor
    target_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    assert any(f & target_flags == target_flags and not f & os.O_DIRECTORY for f in opens)
    monkeypatch.setattr(files_mod.os, "open", real_open)

    real_resolve = files_mod.resolve_in_workdir

    def swap_to(kind: str):
        def resolve(workdir, rel):
            resolved = real_resolve(workdir, rel)
            resolved.unlink()
            if kind == "fifo":
                os.mkfifo(resolved)
            elif kind == "symlink":
                resolved.symlink_to(outside / "secret.txt")
            elif kind == "device":
                resolved.symlink_to("/dev/zero")
            return resolved

        return resolve

    for kind in ("fifo", "symlink", "device"):
        (work / "t.txt").write_text("x")
        monkeypatch.setattr(files_mod, "resolve_in_workdir", swap_to(kind))
        r = put(client, "t.txt", "PWNED")
        assert r.status_code == 400, kind
        assert code(r) in ("not_a_file", "invalid_path"), kind
        assert (outside / "secret.txt").read_text() == "TOP-SECRET"
        (work / "t.txt").unlink(missing_ok=True)
        assert sorted(os.listdir(work)) == ["link.txt", "real.txt"], kind
    # a character device reached directly (not via a symlink) is refused by fstat
    monkeypatch.setattr(files_mod, "resolve_in_workdir", lambda workdir, rel: Path("/dev/zero"))
    r = put(client, "anything", "PWNED")
    assert r.status_code == 400 and code(r) == "not_a_file"


def test_write_atomic_replace(
    client: TestClient, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = work / "script.sh"
    target.write_text("old")
    target.chmod(0o750)
    hard = work / "hard.txt"
    os.link(target, hard)
    inode = target.stat().st_ino
    assert put(client, "script.sh", "new").status_code == 200
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert target.read_text() == "new"
    # the file is replaced, not rewritten: new inode, the hard link keeps the old content
    assert target.stat().st_ino != inode and hard.read_text() == "old"
    assert sorted(os.listdir(work)) == ["hard.txt", "script.sh"]

    target.chmod(0o604)
    assert put(client, "script.sh", "again").status_code == 200
    assert stat.S_IMODE(target.stat().st_mode) == 0o604

    def boom(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(files_mod.os, "write", boom)
    r = put(client, "script.sh", "never lands")
    assert r.status_code == 400
    monkeypatch.undo()
    assert target.read_text() == "again"
    assert stat.S_IMODE(target.stat().st_mode) == 0o604
    assert sorted(os.listdir(work)) == ["hard.txt", "script.sh"]


def test_write_closes_descriptors(
    client: TestClient, work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (work / "f.txt").write_text("x")
    (work / "d").mkdir()
    os.mkfifo(work / "pipe")
    (work / "leak.txt").symlink_to(outside / "secret.txt")
    assert put(client, "f.txt", "warm").status_code == 200
    before = open_fds()
    assert put(client, "f.txt", "ok").status_code == 200
    assert open_fds() == before
    stale = {"mtime": "2001-01-01T00:00:00.000Z", "size": 0}
    for path, status in (
        ("d", 400),
        ("pipe", 400),
        ("leak.txt", 400),
        ("nope.txt", 404),
        ("nope/x.txt", 404),
        ("..", 400),
    ):
        assert put(client, path, "x").status_code == status, path
        assert open_fds() == before, path
    assert put(client, "f.txt", "x", expected=stale).status_code == 409
    assert open_fds() == before

    def boom(fd, data):
        raise OSError(errno.EIO, "I/O error")

    with monkeypatch.context() as m:
        m.setattr(files_mod.os, "write", boom)
        assert put(client, "f.txt", "x").status_code == 400
    assert open_fds() == before
    # an over-cap body is refused before anything is opened
    small = make_client(work, work.parent, file_preview_max_bytes=2)
    assert put(small, "f.txt", "xyz").status_code == 413
    assert open_fds() == before
