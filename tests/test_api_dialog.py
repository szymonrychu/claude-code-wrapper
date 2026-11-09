"""GET and POST /v1/sessions/{id}/dialog over the real router and the fake claude (W8)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from conftest import World, jsonl
from test_api_turns import TOKEN
from test_permission_relay import GATED, requested, resolved, start, tool_results

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.manager import CreateSessionRequest

H = {"Authorization": f"Bearer {TOKEN}"}
RID = "perm_0123456789ab"
USAGE_FOOTER = "Usage limit reached \u00b7 continuing automatically at 3pm \u00b7 esc to cancel"
NONE = {"kind": "none", "since": None, "permission": None, "tail": None}


def api(m: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(m, m.cfg)),
        base_url="http://t",
        headers=H,
    )


def keys(world: World, sid: str) -> list[dict]:
    return jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl")


async def start_dialog(world: World, footer: str, escs: Any):
    """A session with `/help` open as a dialog the auto-dismiss leg leaves alone for 60 s."""
    scen = {
        "commands": {"help": {"dialog": {"footer": footer, "dismiss_stdout": "ok", "escs": escs}}}
    }
    cfg = world.cfg(
        scen,
        server={"auth_token": TOKEN},
        sessions={
            "commands": {
                "client": ["*"],
                "quiet_seconds": 0.2,
                "dialog_grace_seconds": 60,
                "timeout_seconds": {"compact": 90, "default": 90},
            }
        },
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    router.request_command("help", origin="client")
    await world.until(lambda: router.session.dialog_open(), 15)
    return m, sid, router


async def test_get_none_when_idle(world: World) -> None:
    m, sid, _ = await start(world, {"turns": []})
    async with api(m) as c:
        r = await c.get(f"/v1/sessions/{sid}/dialog")
    assert r.status_code == 200 and r.json() == NONE


async def test_get_requires_bearer(world: World) -> None:
    m, sid, _ = await start(world, {"turns": []})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(m, m.cfg)), base_url="http://t"
    ) as c:
        assert (await c.get(f"/v1/sessions/{sid}/dialog")).status_code == 401
        assert (
            await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "dismiss"})
        ).status_code == 401


async def test_get_permission_view(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    async with api(m) as c:
        body = (await c.get(f"/v1/sessions/{sid}/dialog")).json()
    assert body["kind"] == "permission" and body["tail"] is None
    assert body["since"] == view["since"]
    assert body["permission"]["tool_name"] == "Bash"
    assert body["permission"]["request_id"] == view["request_id"]
    assert body["permission"]["expires_at"]
    router.answer_permission(view["request_id"], "allow", None)
    await world.wait_turn(m, sid)


async def test_allow_and_deny_round_trip(world: World) -> None:
    m, sid, router = await start(world, GATED)
    async with api(m) as c:
        await router.submit_turn("go")
        view = await requested(world, sid)
        url = f"/v1/sessions/{sid}/dialog"
        r = await c.post(url, json={"action": "allow", "request_id": view["request_id"]})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "status": "answered",
            "request_id": view["request_id"],
            "behavior": "allow",
        }
        await world.wait_turn(m, sid)
        (result,) = tool_results(router)
        assert result["content"] == "probe" and result["is_error"] is False
        assert (await c.get(url)).json() == NONE


async def test_deny_message_reaches_model(world: World) -> None:
    m, sid, router = await start(world, GATED)
    async with api(m) as c:
        await router.submit_turn("go")
        view = await requested(world, sid)
        rid = view["request_id"]
        body = {"action": "deny", "request_id": rid, "message": "use the staging db"}
        r = await c.post(f"/v1/sessions/{sid}/dialog", json=body)
        assert r.json() == {"status": "answered", "request_id": rid, "behavior": "deny"}
        await world.wait_turn(m, sid)
        (result,) = tool_results(router)
        assert result["content"] == "use the staging db" and result["is_error"] is True
        again = await c.post(f"/v1/sessions/{sid}/dialog", json=body)
        assert again.status_code == 409 and again.json()["error"]["code"] == "permission_resolved"


async def test_answer_after_timeout_is_permission_resolved(world: World) -> None:
    m, sid, router = await start(world, GATED, hold=0.5)
    await router.submit_turn("go")
    view = await requested(world, sid)
    got = await resolved(world, sid)
    assert got["by"] == "timeout"
    async with api(m) as c:
        r = await c.post(
            f"/v1/sessions/{sid}/dialog", json={"action": "allow", "request_id": view["request_id"]}
        )
    assert r.status_code == 409 and r.json()["error"]["code"] == "permission_resolved"
    await world.wait_turn(m, sid)
    (result,) = tool_results(router)  # claude got exactly one decision: the timeout deny
    assert result["is_error"] is True
    (decided,) = jsonl(world.session_dir(sid) / "config" / "fake-permissions.jsonl")
    assert decided["behavior"] == "deny"


async def test_allow_without_request_id_422(world: World) -> None:
    m, sid, _ = await start(world, {"turns": []})
    async with api(m) as c:
        for body in ({"action": "allow"}, {"action": "deny", "message": "no"}):
            r = await c.post(f"/v1/sessions/{sid}/dialog", json=body)
            assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
        bad = {"action": "allow", "request_id": "perm_XYZ"}
        r = await c.post(f"/v1/sessions/{sid}/dialog", json=bad)
        assert r.status_code == 422
        r = await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "dismiss", "force": 1})
        assert r.status_code == 422


async def test_message_too_long_422(world: World) -> None:
    m, sid, _ = await start(world, {"turns": []})
    async with api(m) as c:
        body = {"action": "deny", "request_id": RID, "message": "x" * 2001}
        r = await c.post(f"/v1/sessions/{sid}/dialog", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


async def test_allow_unknown_id_dialog_changed(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "allow", "request_id": RID})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dialog_changed"
    assert len(router.permissions) == 1
    router.answer_permission(view["request_id"], "allow", None)
    await world.wait_turn(m, sid)


async def test_allow_when_none_no_dialog(world: World) -> None:
    m, sid, _ = await start(world, {"turns": []})
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "deny", "request_id": RID})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_dialog"


async def test_unknown_session_404(world: World) -> None:
    m, sid, _ = await start(world, {"turns": []})
    async with api(m) as c:
        r = await c.get("/v1/sessions/ses_nope00000000/dialog")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_allow_footer_only_permission_no_dialog(world: World, monkeypatch: Any) -> None:
    """The hook is dead and the TUI prompt is still drawn: the view shows it with a tail, but
    only dismiss answers it."""
    m, sid, router = await start(world, GATED)
    gone = asyncio.Event()
    real = router.handle_claude_hook

    async def relay(event: str, body: Any, gone: Any = None) -> Any:
        if event != "PermissionRequest":
            return await real(event, body, gone)
        await real(event, body, gone=ev)
        return {"stdout": "", "exit_code": 0}

    ev = gone
    monkeypatch.setattr(router, "handle_claude_hook", relay)
    async with api(m) as c:
        url = f"/v1/sessions/{sid}/dialog"
        view = None
        await router.submit_turn("go")
        view = await requested(world, sid)
        gone.set()
        await resolved(world, sid)
        body = (await c.get(url)).json()
        assert body["kind"] == "permission" and body["permission"] is None
        assert body["tail"] and body["since"]
        r = await c.post(url, json={"action": "allow", "request_id": view["request_id"]})
        assert r.status_code == 409 and r.json()["error"]["code"] == "permission_resolved"
        r = await c.post(url, json={"action": "allow", "request_id": RID})
        assert r.status_code == 409 and r.json()["error"]["code"] == "no_dialog"
        assert keys(world, sid) == []
        r = await c.post(url, json={"action": "dismiss", "expect_since": body["since"]})
        assert r.status_code == 200, r.text
        assert r.json()["escape_sent"] is True
        await world.wait_turn(m, sid, "interrupted")


async def test_dismiss_dialog_closes(world: World) -> None:
    m, sid, router = await start_dialog(world, "Esc to cancel", 1)
    async with api(m) as c:
        url = f"/v1/sessions/{sid}/dialog"
        body = (await c.get(url)).json()
        assert body["kind"] == "dialog" and body["permission"] is None
        assert "Esc to cancel" in body["tail"] and body["since"]
        r = await c.post(url, json={"action": "dismiss", "expect_since": body["since"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "dismissed", "escape_sent": True}
    assert len(keys(world, sid)) == 1


async def test_dismiss_without_expect_since(world: World) -> None:
    m, sid, _ = await start_dialog(world, "Esc to cancel", 1)
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "dismiss"})
    assert r.json() == {"status": "dismissed", "escape_sent": True}


async def test_dismiss_when_none_no_dialog(world: World) -> None:
    m, sid, _ = await start(world, {"turns": []})
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "dismiss"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_dialog"


async def test_dismiss_twice_sends_one_escape(world: World) -> None:
    m, sid, _ = await start_dialog(world, "Esc to cancel", 2)
    async with api(m) as c:
        url = f"/v1/sessions/{sid}/dialog"
        since = (await c.get(url)).json()["since"]
        body = {"action": "dismiss", "expect_since": since}
        first, second = await asyncio.gather(c.post(url, json=body), c.post(url, json=body))
    answers = sorted([first.json(), second.json()], key=lambda a: a["escape_sent"])
    assert [a["status"] for a in answers] == ["dismissed", "dismissed"]
    assert answers[0]["escape_sent"] is False
    assert answers[1]["escape_sent"] is True
    assert len(keys(world, sid)) == 1


async def test_dismiss_reports_still_open(world: World, monkeypatch: Any) -> None:
    """The ESC is refused by the write guard (the dialog moved): the dialog stays open and the
    answer says so after the confirmation window."""
    m, sid, router = await start_dialog(world, "Esc to cancel", 1)
    monkeypatch.setattr("claude_code_wrapper.router.DISMISS_CONFIRM_SECONDS", 0.3)

    async def refused(since: str) -> bool:
        return False

    monkeypatch.setattr(router.session, "escape_dialog", refused)
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "dismiss"})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "still_open", "escape_sent": False}
    assert keys(world, sid) == []


async def test_dismiss_stale_since_dialog_changed(world: World) -> None:
    m, sid, _ = await start_dialog(world, "Esc to cancel", 1)
    async with api(m) as c:
        r = await c.post(
            f"/v1/sessions/{sid}/dialog",
            json={"action": "dismiss", "expect_since": "2020-01-01T00:00:00.000Z"},
        )
    assert r.status_code == 409 and r.json()["error"]["code"] == "dialog_changed"
    assert keys(world, sid) == []


async def test_dismiss_permission_not_dismissable(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/dialog", json={"action": "dismiss"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_dismissable"
    assert keys(world, sid) == []
    router.answer_permission(view["request_id"], "allow", None)
    await world.wait_turn(m, sid)


async def test_dismiss_usage_wait_not_dismissable(world: World) -> None:
    m, sid, _ = await start_dialog(world, USAGE_FOOTER, "never")
    async with api(m) as c:
        url = f"/v1/sessions/{sid}/dialog"
        body = (await c.get(url)).json()
        assert body["kind"] == "usage_wait" and body["tail"] is None and body["since"]
        r = await c.post(url, json={"action": "dismiss"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_dismissable"
    assert keys(world, sid) == []


async def test_tail_redacted(world: World) -> None:
    secret = "sk-ant-oat01-supersecretvalue"
    m, sid, _ = await start_dialog(world, f"{secret} Esc to cancel", 1)
    m.redactor.add([secret])
    async with api(m) as c:
        body = (await c.get(f"/v1/sessions/{sid}/dialog")).json()
    assert body["kind"] == "dialog"
    assert secret not in body["tail"] and "***" in body["tail"]
    assert len(body["tail"]) <= 1500
