"""Session file API: listing, content, confinement and auth over a stub manager."""

from __future__ import annotations

import os
import re
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
