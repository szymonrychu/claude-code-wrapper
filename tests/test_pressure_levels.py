"""Context pressure: level evaluation, text rendering, per-session policy state."""

from __future__ import annotations

import pytest

from claude_code_wrapper.pressure import (
    LevelTexts,
    PressurePolicy,
    PressureState,
    StaleVersion,
    Thresholds,
    evaluate_level,
    render,
)

TH = Thresholds(
    l1_tokens=100_000,
    l2_tokens=130_000,
    l3_tokens=165_000,
    compact_tokens=190_000,
    l1_pct=50.0,
    l2_pct=65.0,
    l3_pct=82.5,
    compact_pct=95.0,
)


def ctx(used=None, window=200_000, pct=None):
    return {"used_tokens": used, "window_tokens": window, "used_percentage": pct}


def test_level_from_tokens() -> None:
    for used, level in [(99_999, 0), (100_000, 1), (130_000, 2), (165_000, 3), (200_000, 3)]:
        assert evaluate_level(TH, ctx(used)) == level


TH25 = Thresholds(
    l1_tokens=None,
    l2_tokens=0,
    l3_tokens=25_000,
    compact_tokens=190_000,
    l2_pct=15.0,
    l3_pct=20.0,
    compact_pct=95.0,
)
TH1M = Thresholds(
    l1_tokens=None,
    l2_tokens=150_000,
    l3_tokens=200_000,
    compact_tokens=900_000,
    l2_pct=15.0,
    l3_pct=20.0,
    compact_pct=90.0,
)


@pytest.mark.parametrize(
    ("th", "gauge", "level"),
    [
        pytest.param(TH, ctx(window=None, pct=71), 2, id="pct-fallback-without-window"),
        pytest.param(TH25, ctx(39_000, None, 3.9), 0, id="mx-973-tokens-ignored-without-window"),
        pytest.param(TH25, ctx(39_000, None, 21), 3, id="pct-decides-without-window"),
        pytest.param(TH1M, ctx(39_000, 1_000_000), 0, id="1m-below-l2"),
        pytest.param(TH1M, ctx(160_000, 1_000_000), 2, id="1m-l2"),
        pytest.param(TH1M, ctx(210_000, 1_000_000), 3, id="1m-l3"),
        pytest.param(TH1M, ctx(None, 1_000_000, 50), 0, id="window-known-used-unknown"),
    ],
)
def test_level_rule_matrix(th, gauge, level) -> None:
    assert evaluate_level(th, gauge) == level


def test_unknown_context_is_level_zero() -> None:
    assert evaluate_level(TH, None) == 0
    assert evaluate_level(TH, ctx()) == 0


def test_l1_none_skips_level_one() -> None:
    th = TH.model_copy(update={"l1_tokens": None})
    assert evaluate_level(th, ctx(110_000)) == 0
    assert evaluate_level(th, ctx(130_000)) == 2


def test_render_fills_live_numbers() -> None:
    tpl = (
        "Context {pct}% ({used_k}k of {window_k}k tokens). "
        "Auto-compaction at {compact_pct}% (~{left_k}k tokens left)."
    )
    out = render(tpl, 2, ctx(142_000), TH, [])
    assert out == "Context 71% (142k of 200k tokens). Auto-compaction at 95% (~48k tokens left)."


def test_render_unknown_placeholder_kept_literal() -> None:
    assert render("a {nope} {level} {x.y} {", 2, ctx(142_000), TH, []) == "a {nope} 2 {x.y} {"


def test_render_missing_values_and_waiting_on() -> None:
    out = render("{pct} {used_k} {left_k} {waiting_on}", 1, ctx(window=None), TH, ["ci", "sub"])
    assert out == "? ? ? ci, sub"
    # used known, no window: compact_tokens was computed for an assumed window
    assert render("{left_k}", 1, ctx(39_000, None, 3.9), TH, []) == "?"


def _policy(version: int = 1, **kw) -> PressurePolicy:
    texts = {lv: LevelTexts(full="f", short="s") for lv in ("l1", "l2", "l3")}
    return PressurePolicy(version=version, thresholds=TH, profile="work", texts=texts, **kw)


def test_put_same_version_unchanged_lower_version_stale() -> None:
    st = PressureState()
    assert st.put(_policy(2)) == "set"
    assert st.put(_policy(2)) == "unchanged"
    assert st.put(_policy(3)) == "set"
    with pytest.raises(StaleVersion):
        st.put(_policy(2))
    st.clear()
    assert st.policy is None
    assert st.view(None)["version"] is None


def test_same_version_different_body_is_set() -> None:
    st = PressureState()
    st.put(_policy(2))
    assert st.put(_policy(2, waiting_on=["x"])) == "set"


def test_render_pct_uses_tokens_when_window_known() -> None:
    # used_percentage relative to another window must not mix with {compact_pct}
    out = render("{pct} {compact_pct}", 2, ctx(142_000, pct=90.0), TH, [])
    assert out == "71 95"


def test_profile_is_a_free_label() -> None:
    # The wrapper only logs the profile: any short label a deployment uses is accepted.
    texts = {lv: LevelTexts(full="f", short="s") for lv in ("l1", "l2", "l3")}
    base = dict(version=1, thresholds=TH, texts=texts)
    for label in ("work", "chat", "batch", "night-shift.v2"):
        assert PressurePolicy(**base, profile=label).profile == label
    for bad in ("", "a b", "x" * 65, "a\nb"):
        with pytest.raises(ValueError):
            PressurePolicy(**base, profile=bad)


def test_waiting_on_is_capped() -> None:
    texts = {lv: LevelTexts(full="f", short="s") for lv in ("l1", "l2", "l3")}
    base = dict(version=1, thresholds=TH, profile="work", texts=texts)
    with pytest.raises(ValueError):
        PressurePolicy(**base, waiting_on=["x"] * 21)
    with pytest.raises(ValueError):
        PressurePolicy(**base, waiting_on=["x" * 201])
