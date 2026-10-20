"""Webhook actions, signing, retries and post_json."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import httpx

from claude_code_wrapper import hooks
from claude_code_wrapper.config import HooksConfig
from claude_code_wrapper.hooks import HookDispatcher, build_payload
from claude_code_wrapper.redact import Redactor


def payload():
    return build_payload(
        "onTurn",
        session_id="s",
        claude_session_id=None,
        turn_id="t",
        workdir="/w",
        data={"k": "supersecretvalue"},
    )


def wh(**kw):
    return {"type": "webhook", "url": "http://hook.test/x", "blocking": True, **kw}


def make(tmp_path, hooks_cfg, handler):
    results: list = []
    d = HookDispatcher(
        HooksConfig.model_validate({"onTurn": hooks_cfg}),
        Redactor(["supersecretvalue"]),
        base_env={"PATH": os.environ["PATH"]},
        default_cwd=tmp_path,
        transport=httpx.MockTransport(handler),
        on_result=results.append,
    )
    return d, results


async def test_webhook_signature_hmac_sha256(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200)

    d, results = make(tmp_path, [wh(secret="s3cr3t-key", headers={"X-A": "1"})], handler)
    o = await d.fire("onTurn", payload())
    assert o.decision == "allow" and results[0].ok
    r = seen[0]
    assert r.headers["content-type"] == "application/json"
    assert r.headers["x-ccw-event"] == "onTurn" and r.headers["x-a"] == "1"
    assert len(r.headers["x-ccw-delivery"]) == 36
    sig = hmac.new(b"s3cr3t-key", r.content, hashlib.sha256).hexdigest()
    assert r.headers["x-ccw-signature"] == f"sha256={sig}"
    assert b"supersecretvalue" not in r.content and json.loads(r.content)["data"]["k"] == "***"


async def test_no_signature_without_secret(tmp_path):
    seen = []
    d, _ = make(tmp_path, [wh()], lambda req: seen.append(req) or httpx.Response(204))
    await d.fire("onTurn", payload())
    assert "x-ccw-signature" not in seen[0].headers


async def test_blocking_webhook_deny_response(tmp_path):
    body = {"decision": "deny", "reason": "no", "additional_context": "ctx"}
    d, _ = make(tmp_path, [wh()], lambda req: httpx.Response(200, json=body))
    o = await d.fire("onTurn", payload())
    assert o.decision == "deny" and o.reason == "no" and o.additional_context == "ctx"


async def test_non_json_2xx_allows(tmp_path):
    d, _ = make(tmp_path, [wh()], lambda req: httpx.Response(200, text="ok"))
    assert (await d.fire("onTurn", payload())).decision == "allow"


async def test_webhook_retries_on_5xx(tmp_path, monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(hooks.asyncio, "sleep", fake_sleep)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(500) if len(calls) == 2 else httpx.Response(200)

    d, results = make(tmp_path, [wh(retries=3)], handler)
    o = await d.fire("onTurn", payload())
    assert len(calls) == 3 and results[0].ok and o.decision == "allow"
    assert sleeps == [0.5, 1.0]


async def test_webhook_failure_on_error_deny(tmp_path, monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(hooks.asyncio, "sleep", fake_sleep)
    calls = []
    d, results = make(
        tmp_path,
        [wh(retries=2, on_error="deny")],
        lambda req: calls.append(1) or httpx.Response(503),
    )
    o = await d.fire("onTurn", payload())
    assert len(calls) == 3 and o.decision == "deny"
    assert not results[0].ok and "503" in results[0].error


async def test_nonblocking_failure_is_logged_not_raised(tmp_path):
    d, results = make(tmp_path, [wh(blocking=False, retries=0)], lambda req: httpx.Response(500))
    o = await d.fire("onTurn", payload())
    assert o.results == []
    await d.drain(5)
    assert len(results) == 1 and not results[0].ok


async def test_post_json_signed(tmp_path):
    seen = []
    t = httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200))
    ok = await hooks.post_json("http://cb.test/", {"a": 1}, secret="k", retries=0, transport=t)
    assert ok is True
    sig = hmac.new(b"k", seen[0].content, hashlib.sha256).hexdigest()
    assert seen[0].headers["x-ccw-signature"] == f"sha256={sig}"


async def test_dispatcher_post_json_and_failure(tmp_path, monkeypatch):
    d, _ = make(tmp_path, [], lambda req: httpx.Response(500))
    assert await d.post_json("http://cb.test/", {"a": 1}, retries=0) is False


async def test_payload_redacted(tmp_path):
    seen = []
    d, _ = make(tmp_path, [wh()], lambda req: seen.append(req) or httpx.Response(200))
    await d.fire("onTurn", payload())
    assert b"supersecretvalue" not in seen[0].content
    assert json.loads(seen[0].content)["data"]["k"] == "***"


async def test_on_result_called(tmp_path):
    d, results = make(
        tmp_path,
        [wh(), wh(retries=0, on_error="deny")],
        lambda req: httpx.Response(200),
    )
    await d.fire("onTurn", payload())
    assert sorted(r.index for r in results) == [0, 1]
    assert all(r.type == "webhook" and r.event == "onTurn" for r in results)


async def test_webhook_total_time_bounded_by_timeout(tmp_path):
    def handler(req):
        return httpx.Response(503)

    d, results = make(tmp_path, [wh(retries=5, timeout_seconds=1, on_error="deny")], handler)
    t0 = time.monotonic()
    o = await d.fire("onTurn", payload())
    assert time.monotonic() - t0 < 2.5
    assert o.decision == "deny" and not results[0].ok and "timeout" in results[0].error
