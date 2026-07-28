"""Storm suppression. Every limit is exercised with an injected clock.

The scenario that matters is the one PLAN.md §7 names: a crash loop must not
flood GitHub, and a bad deploy must not open fifty issues at once.
"""

from __future__ import annotations

from err2issue.suppress import Suppressor
from tests.conftest import FakeClock


def build(clock: FakeClock, **kwargs) -> Suppressor:
    defaults = dict(window_seconds=600, max_per_minute=30, max_new_per_day=50)
    defaults.update(kwargs)
    return Suppressor(clock=clock, wall_clock=lambda: 0.0, **defaults)


# -- per-fingerprint window ------------------------------------------------


def test_first_occurrence_is_allowed(clock):
    assert build(clock).check("aaa")


def test_second_occurrence_inside_the_window_is_suppressed(clock):
    suppressor = build(clock)
    assert suppressor.check("aaa")
    decision = suppressor.check("aaa")
    assert not decision
    assert "suppression window" in decision.reason


def test_occurrence_after_the_window_is_allowed_again(clock):
    suppressor = build(clock, window_seconds=600)
    assert suppressor.check("aaa")
    clock.advance(601)
    assert suppressor.check("aaa")


def test_the_window_is_per_fingerprint_not_global(clock):
    suppressor = build(clock)
    assert suppressor.check("aaa")
    assert suppressor.check("bbb"), "a different error must not be blocked by another's window"


def test_a_crash_loop_produces_exactly_one_filing_per_window(clock):
    """The headline requirement: 1000 occurrences, one filing."""
    suppressor = build(clock, window_seconds=600, max_per_minute=1000, max_new_per_day=1000)
    allowed = sum(1 for _ in range(1000) if suppressor.check("crashloop"))
    assert allowed == 1


# -- global rate cap -------------------------------------------------------


def test_global_cap_limits_distinct_fingerprints_per_minute(clock):
    suppressor = build(clock, max_per_minute=5, max_new_per_day=1000)
    allowed = sum(1 for i in range(20) if suppressor.check(f"fp{i}"))
    assert allowed == 5


def test_the_token_bucket_refills_over_time(clock):
    suppressor = build(clock, max_per_minute=6, max_new_per_day=1000)
    for i in range(6):
        assert suppressor.check(f"fp{i}")
    assert not suppressor.check("fp-blocked")
    clock.advance(30)  # half a minute at 6/min == 3 tokens
    allowed = sum(1 for i in range(10) if suppressor.check(f"later{i}"))
    assert allowed == 3


def test_rate_cap_reason_is_reported(clock):
    suppressor = build(clock, max_per_minute=1, max_new_per_day=1000)
    suppressor.check("a")
    assert "global rate cap" in suppressor.check("b").reason


# -- daily new-fingerprint budget (bad-deploy guard) -----------------------


def test_new_fingerprint_budget_caps_distinct_errors_per_day(clock):
    suppressor = build(clock, max_new_per_day=3, max_per_minute=1000)
    allowed = sum(1 for i in range(10) if suppressor.check(f"new{i}"))
    assert allowed == 3


def test_known_fingerprints_still_pass_after_the_daily_budget_is_spent(clock):
    """A bad deploy must not silence errors we were already tracking."""
    suppressor = build(clock, max_new_per_day=1, window_seconds=60, max_per_minute=1000)
    assert suppressor.check("known")
    assert not suppressor.check("brand-new")
    clock.advance(61)
    assert suppressor.check("known"), "recurrence of a known error must survive the budget"


def test_daily_budget_resets_on_the_next_day():
    clock = FakeClock()
    wall = {"t": 0.0}
    suppressor = Suppressor(
        window_seconds=1,
        max_per_minute=1000,
        max_new_per_day=2,
        clock=clock,
        wall_clock=lambda: wall["t"],
    )
    assert suppressor.check("a")
    assert suppressor.check("b")
    assert not suppressor.check("c")
    wall["t"] += 86_400
    clock.advance(86_400)
    assert suppressor.check("c")


# -- ordering of checks ----------------------------------------------------


def test_window_is_checked_before_budgets_so_repeats_do_not_burn_quota(clock):
    suppressor = build(clock, max_per_minute=10, max_new_per_day=10)
    suppressor.check("aaa")
    for _ in range(50):
        suppressor.check("aaa")
    # Only the first call consumed a token, so nine remain for other errors.
    allowed = sum(1 for i in range(9) if suppressor.check(f"other{i}"))
    assert allowed == 9


# -- bookkeeping -----------------------------------------------------------


def test_tracking_table_is_bounded(clock):
    from err2issue.suppress import MAX_TRACKED_FINGERPRINTS

    suppressor = build(clock, max_per_minute=10**9, max_new_per_day=10**9)
    for i in range(MAX_TRACKED_FINGERPRINTS + 500):
        suppressor.check(f"fp{i}")
    assert suppressor.stats()["tracked_fingerprints"] <= MAX_TRACKED_FINGERPRINTS


def test_stats_reports_useful_counters(clock):
    suppressor = build(clock)
    suppressor.check("aaa")
    stats = suppressor.stats()
    assert stats["tracked_fingerprints"] == 1
    assert stats["new_today"] == 1


def test_decision_is_truthy_and_falsy(clock):
    suppressor = build(clock)
    assert bool(suppressor.check("aaa")) is True
    assert bool(suppressor.check("aaa")) is False
