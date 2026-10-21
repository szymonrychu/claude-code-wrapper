"""K13: HTTP, probe, stall and interrupt metric families."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter
from fastapi.testclient import TestClient
from test_metrics import ANON, sample, with_metrics
from test_router import Harness, completed_record

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.manager import InvalidState
from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.session import TurnRejected
from claude_code_wrapper.transcript import AssistantText, ProbeMarker
from claude_code_wrapper.turns import Probe, now_iso


class ProbeSession:
    def __init__(self, reject: bool = False) -> None:
        self.reject = reject

    async def probe(self, text=None):
        if self.reject:
            raise TurnRejected("booting")
        return Probe(probe_id="p1", text="hi", created_at=now_iso())


class Stub:
    started = True
    redactor = Redactor()

    def __init__(self, session: ProbeSession | None = None) -> None:
        self._session = session

    def router(self, session_id: str):
        if self._session is None:
            raise InvalidState("dead")
        return SimpleNamespace(session=self._session)


def client_for(m: Metrics, stub: Stub | None = None) -> TestClient:
    app = create_app(stub or Stub(), ANON, metrics=m)
    boom = APIRouter()

    @boom.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    app.include_router(boom)
    return TestClient(app, raise_server_exceptions=False)


def test_http_requests_labelled_by_route_template() -> None:
    m = Metrics()
    c = client_for(m, Stub(ProbeSession()))
    assert c.post("/v1/sessions/abc/probe").status_code == 202
    assert (
        sample(
            m,
            "ccw_http_requests_total",
            route="/v1/sessions/{session_id}/probe",
            method="POST",
            status_code="202",
        )
        == 1
    )
    assert c.get("/healthz").status_code == 200
    assert sample(m, "ccw_http_requests_total", route="/healthz", method="GET", status_code="200")


def test_http_unmatched_route_is_bounded() -> None:
    m = Metrics()
    c = client_for(m)
    assert c.get("/nope/123").status_code == 404
    assert c.get("/nope/456").status_code == 404
    assert (
        sample(m, "ccw_http_requests_total", route="unmatched", method="GET", status_code="404")
        == 2
    )


def test_http_panic_counted() -> None:
    m = Metrics()
    c = client_for(m)
    assert c.get("/boom").status_code == 500
    assert sample(m, "ccw_http_panics_total") == 1
    assert sample(m, "ccw_http_requests_total", route="/boom", method="GET", status_code="500")


def test_probe_sent_and_rejected_outcomes() -> None:
    m = Metrics()
    c = client_for(m, Stub(ProbeSession()))
    c.post("/v1/sessions/abc/probe")
    assert sample(m, "ccw_probe_outcomes_total", outcome="sent") == 1
    c = client_for(m, Stub(ProbeSession(reject=True)))
    assert c.post("/v1/sessions/abc/probe").status_code != 202
    assert sample(m, "ccw_probe_outcomes_total", outcome="rejected") == 1
    assert sample(m, "ccw_probe_outcomes_total", outcome="sent") == 1


def test_probe_to_dead_session_counted_rejected() -> None:
    m = Metrics()
    c = client_for(m, Stub(None))
    assert c.post("/v1/sessions/abc/probe").status_code == 503
    assert sample(m, "ccw_probe_outcomes_total", outcome="rejected") == 1


async def test_probe_outcomes_and_answer_seconds(tmp_path: Path) -> None:
    m = Metrics()
    h = with_metrics(Harness(tmp_path), m)
    probe = Probe(probe_id="p1", text="hi", created_at="2026-01-01T00:00:00+00:00")
    h.session.probes["p1"] = probe
    orig = h.session.probe_answered

    def answered(answer=""):
        out = orig(answer)
        for p in out:
            p.answered_at = "2026-01-01T00:00:02+00:00"
        return out

    h.session.probe_answered = answered  # type: ignore[method-assign]
    token = h.cfg.sessions.probe_alive_token
    await h.router._on_transcript_events([ProbeMarker(probe_id="p1")])
    await h.router._on_transcript_events([AssistantText(text=token)])
    await h.router.close()
    assert sample(m, "ccw_probe_outcomes_total", outcome="delivered") == 1
    assert sample(m, "ccw_probe_outcomes_total", outcome="answered") == 1
    assert sample(m, "ccw_probe_answer_seconds_count") == 1
    assert sample(m, "ccw_probe_answer_seconds_sum") == 2.0


async def test_stall_counted(tmp_path: Path) -> None:
    m = Metrics()
    h = with_metrics(Harness(tmp_path), m)
    h.router.on_stall("t1")
    assert sample(m, "ccw_stall_suspected_total") == 1
    await h.router.close()


async def test_interrupt_results_counted(tmp_path: Path) -> None:
    m = Metrics()
    h = with_metrics(Harness(tmp_path), m)
    h.session.interrupt_result = None
    await h.router.interrupt()
    assert sample(m, "ccw_interrupts_total", result="idle") == 1
    h.session.interrupt_result = "t1"
    await h.router.interrupt()
    assert sample(m, "ccw_interrupts_total", result="interrupted") == 1
    h.router.on_turn_end(completed_record(state="timeout", completed_at=now_iso()))
    await h.router.drain()
    assert sample(m, "ccw_interrupts_total", result="timeout") == 1
    await h.router.close()
