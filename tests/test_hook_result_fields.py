"""Hook result fields: annotations, output, per-event decision table, caps."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from claude_code_wrapper.config import VETO_EVENTS, HooksConfig
from claude_code_wrapper.hooks import (
    EVENT_DECISIONS,
    OUTPUT_CAP_BYTES,
    HookDispatcher,
    build_payload,
)
from claude_code_wrapper.redact import Redactor


def make(tmp_path: Path, hooks: dict, **kw):
    results: list = []
    d = HookDispatcher(
        HooksConfig.model_validate(hooks),
        Redactor(["supersecretvalue"]),
        base_env={"PATH": os.environ["PATH"]},
        default_cwd=tmp_path,
        on_result=results.append,
        **kw,
    )
    return d, results


def payload(event="onTurn"):
    return build_payload(
        event, session_id="s", claude_session_id="c", turn_id="t", workdir="/w", data={}
    )


def cmd(obj, **kw):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return {"type": "command", "run": f"printf '%s' '{text}'", "blocking": True, **kw}


def test_event_decisions_table():
    for ev in VETO_EVENTS:
        assert EVENT_DECISIONS[ev] == ("allow", "deny")
    assert EVENT_DECISIONS["onTurnComplete"] == ("allow",)
    assert EVENT_DECISIONS["onDestroy"] == ("allow",)


async def test_annotations_and_output_parsed(tmp_path):
    d, _ = make(
        tmp_path,
        {"onTurn": [cmd({"annotations": {"a": 1}, "output": [1, {"x": 2}]})]},
    )
    o = await d.fire("onTurn", payload())
    assert o.results[0].annotations == {"a": 1}
    assert o.results[0].output == [1, {"x": 2}]
    assert o.annotations == {"a": 1}
    assert o.outputs == [{"index": 0, "ok": True, "output": [1, {"x": 2}], "error": None}]


async def test_annotations_merge_in_index_order_later_wins(tmp_path):
    d, _ = make(
        tmp_path,
        {
            "onTurn": [
                cmd({"annotations": {"a": 1, "b": 1}}),
                cmd({"annotations": {"b": 2}}),
                cmd({"output": "x"}),
            ]
        },
    )
    o = await d.fire("onTurn", payload())
    assert o.annotations == {"a": 1, "b": 2}
    assert [x["index"] for x in o.outputs] == [0, 1, 2]
    assert o.outputs[2]["output"] == "x"


async def test_non_annotation_types_ignored(tmp_path):
    d, _ = make(tmp_path, {"onTurn": [cmd({"annotations": [1], "output": None})]})
    o = await d.fire("onTurn", payload())
    assert o.annotations == {}
    assert o.results[0].annotations is None and o.results[0].output is None


async def test_non_blocking_fields_ignored(tmp_path):
    d, results = make(
        tmp_path, {"onTurn": [cmd({"annotations": {"a": 1}, "output": 5}, blocking=False)]}
    )
    o = await d.fire("onTurn", payload())
    await d.drain(5)
    assert o.annotations == {} and o.outputs == []
    assert results[0].annotations is None and results[0].output is None


async def test_output_redacted_before_event(tmp_path):
    d, results = make(tmp_path, {"onTurn": [cmd({"output": {"k": "supersecretvalue"}})]})
    o = await d.fire("onTurn", payload())
    assert "supersecretvalue" not in json.dumps(o.outputs)
    assert "supersecretvalue" not in json.dumps(results[0].output)


async def test_output_cap_truncates(tmp_path):
    script = tmp_path / "big.py"
    script.write_text(f"import json;print(json.dumps({{'output': 'a'*{OUTPUT_CAP_BYTES + 10}}}))")
    d, _ = make(
        tmp_path,
        {"onTurn": [{"type": "command", "run": f"python3 {script}", "blocking": True}]},
    )
    o = await d.fire("onTurn", payload())
    out = o.results[0].output
    assert out["truncated"] is True and out["bytes"] > OUTPUT_CAP_BYTES
    assert set(out) == {"truncated", "bytes"}


async def test_annotations_over_cap_dropped_with_error(tmp_path):
    script = tmp_path / "big.py"
    script.write_text("import json;print(json.dumps({'annotations': {'k': 'a'*17000}}))")
    d, _ = make(
        tmp_path,
        {"onTurn": [{"type": "command", "run": f"python3 {script}", "blocking": True}]},
    )
    o = await d.fire("onTurn", payload())
    assert o.results[0].annotations is None
    assert o.results[0].error and "annotations" in o.results[0].error
    assert o.annotations == {}


async def test_block_defer_not_honoured_on_existing_events(tmp_path):
    d, _ = make(tmp_path, {"onTurn": [cmd({"decision": "block"})]})
    o = await d.fire("onTurn", payload())
    assert o.decision == "allow"
    assert o.results[0].decision == "allow"
    assert o.results[0].invalid_decision == "block"


async def test_deny_on_non_veto_records_invalid(tmp_path):
    d, _ = make(tmp_path, {"onTurnComplete": [cmd({"decision": "deny"})]})
    o = await d.fire("onTurnComplete", payload("onTurnComplete"))
    assert o.decision == "allow"
    assert o.results[0].decision == "allow"
    assert o.results[0].invalid_decision == "deny"


async def test_deny_on_veto_still_vetoes(tmp_path):
    d, _ = make(tmp_path, {"onTurn": [cmd({"decision": "deny", "reason": "r"})]})
    o = await d.fire("onTurn", payload())
    assert o.decision == "deny" and o.results[0].invalid_decision is None


async def test_webhook_body_fields(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"annotations": {"w": 1}, "output": {"o": True}})

    d, _ = make(
        tmp_path,
        {"onTurn": [{"type": "webhook", "url": "http://h.test/x", "blocking": True}]},
        transport=httpx.MockTransport(handler),
    )
    o = await d.fire("onTurn", payload())
    assert o.annotations == {"w": 1}
    assert o.outputs[0]["output"] == {"o": True}


async def test_failed_action_listed_in_outputs(tmp_path):
    d, _ = make(tmp_path, {"onTurn": [{"type": "command", "run": "exit 3", "blocking": True}]})
    o = await d.fire("onTurn", payload())
    assert o.outputs[0]["ok"] is False and "exit 3" in o.outputs[0]["error"]
    assert o.outputs[0]["output"] is None
