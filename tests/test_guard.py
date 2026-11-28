import pytest

from claude_code_wrapper.config import Config, GuardConfig
from claude_code_wrapper.guard import BreakerVerdict, RunawayGuard, SpawnVerdict, call_key


@pytest.mark.parametrize(
    "field,bad",
    [
        ("max_stop_continuations", 1001),
        ("max_agent_spawns", 100001),
        ("tool_failure_repeats", 1),
        ("tool_failure_repeats", 101),
        ("max_agent_spawns", -1),
    ],
)
def test_guard_config_bounds(field, bad):
    with pytest.raises(ValueError, match=field):
        Config.model_validate({"guard": {field: bad}})


def test_stop_continuation_counts_active_only_and_trips_at_cap():
    g = RunawayGuard(GuardConfig(max_stop_continuations=3))
    assert g.stop_continuation("t1", False) is None
    assert [g.stop_continuation("t1", True) for _ in range(2)] == [None, None]
    assert g.stop_continuation("t1", True) == 3 and g.loop_tripped
    assert g.stop_continuation("t1", True) == 3
    g.clear_loop_trip()
    assert not g.loop_tripped


def test_stop_continuation_resets_only_on_new_turn():
    g = RunawayGuard(GuardConfig(max_stop_continuations=3))
    g.stop_continuation("t1", True)
    g.stop_continuation("t1", True)
    assert g.stop_continuation("t2", True) is None
    assert g.stop_continuation("t2", True) is None
    assert g.stop_continuation("t2", True) == 3


def test_stop_continuation_off_when_zero():
    g = RunawayGuard(GuardConfig())
    assert all(g.stop_continuation("t", True) is None for _ in range(50))
    assert not g.loop_tripped


def test_spawn_counts_per_claude_session():
    g = RunawayGuard(GuardConfig(max_agent_spawns=2))
    assert [g.spawn("c1") for _ in range(4)] == [
        SpawnVerdict(True, 1, False),
        SpawnVerdict(True, 2, False),
        SpawnVerdict(False, 2, True),
        SpawnVerdict(False, 2, False),
    ]
    assert g.spawn("c2") == SpawnVerdict(True, 1, False)


def test_spawn_off_when_zero():
    g = RunawayGuard(GuardConfig())
    assert g.spawn("c1") == SpawnVerdict(True, 0, False)


def test_call_key_ignores_key_order_and_tolerates_odd_values():
    assert call_key(None, "Edit", {"a": 1, "b": 2}) == call_key(None, "Edit", {"b": 2, "a": 1})
    assert call_key(None, "Edit", {})[0] == "main"
    assert call_key("a1", "Edit", {})[0] == "a1"
    call_key(None, "Bash", {"x": {1, 2}, "y": object()})


def _fail(g, k, n, prefix="u"):
    for i in range(n):
        g.record_result(k, failed=True, tool_use_id=f"{prefix}{i}")


def test_breaker_trips_after_threshold_failures_once_per_streak():
    g = RunawayGuard(GuardConfig(tool_failure_repeats=3))
    k = call_key(None, "Bash", {"command": "false"})
    _fail(g, k, 3)
    assert g.breaker_check(k) == BreakerVerdict(True, 3, True)
    g.record_denial(k, "u3")
    assert g.breaker_check(k) == BreakerVerdict(True, 4, False)


def test_breaker_success_or_different_call_resets():
    g = RunawayGuard(GuardConfig(tool_failure_repeats=3))
    k = call_key(None, "Bash", {"command": "false"})
    k2 = call_key(None, "Bash", {"command": "other"})
    _fail(g, k, 2)
    g.record_result(k, failed=False, tool_use_id="ok")
    assert not g.breaker_check(k).deny
    _fail(g, k, 2, "v")
    g.record_result(k2, failed=True, tool_use_id="w")
    assert g.streak("main") == 1
    assert not g.breaker_check(k).deny


def test_breaker_ignores_failure_for_denied_tool_use_id():
    g = RunawayGuard(GuardConfig(tool_failure_repeats=3))
    k = call_key(None, "Bash", {"command": "false"})
    g.record_denial(k, "tu1")
    g.record_result(k, failed=True, tool_use_id="tu1")
    assert g.streak("main") == 1


def test_breaker_per_agent():
    g = RunawayGuard(GuardConfig(tool_failure_repeats=3))
    _fail(g, call_key("a1", "Bash", {"command": "false"}), 3)
    assert not g.breaker_check(call_key(None, "Bash", {"command": "false"})).deny
    assert g.breaker_check(call_key("a1", "Bash", {"command": "false"})).deny


def test_breaker_off_when_zero():
    g = RunawayGuard(GuardConfig())
    k = call_key(None, "Bash", {"command": "false"})
    _fail(g, k, 10)
    assert not g.breaker_check(k).deny
