"""The admin dashboard's average latency follows recent actions.

A plain mean over the last 5000 audit events hardly moves when the service gets slower or faster,
so ``AuthStore.stats()`` weighs each event half as much as the one ``LATENCY_HALF_LIFE_ACTIONS``
newer (``app.auth.recency_weighted_average``). Fully offline.
"""

from __future__ import annotations

from app.auth import LATENCY_HALF_LIFE_ACTIONS, NOBODY, AuthStore, recency_weighted_average
from increment1_support import BOOTSTRAP


def test_no_values_give_zero():
    assert recency_weighted_average([]) == 0


def test_equal_values_give_that_value():
    assert recency_weighted_average([500] * 50) == 500


def test_a_value_weighs_half_as_much_as_the_one_a_half_life_newer():
    # Weights 1 and 1/2: (900 + 150) / 1.5.
    assert recency_weighted_average([900, 300], half_life=1) == 700


def test_the_last_half_life_of_actions_carries_half_the_weight():
    recent_slow = [30_000] * LATENCY_HALF_LIFE_ACTIONS
    older_fast = [100] * 4000
    values = recent_slow + older_fast  # newest first
    assert round(sum(values) / len(values)) < 300  # what a plain mean would show
    assert abs(recency_weighted_average(values) - 15_050) <= 5


def test_the_dashboard_figure_weights_the_newest_events_most(tmp_path, monkeypatch):
    for name, value in BOOTSTRAP.items():  # an empty database starts only with a first admin (D-045)
        monkeypatch.setenv(name, value)
    store = AuthStore(db_path=tmp_path / "auth.db", enabled=True, session_secret="x", cookie_secure=False)
    store.init()
    for _ in range(30):
        store.audit_event(action="decompose", user=NOBODY, status_code=200, latency_ms=100)
    for _ in range(3):
        store.audit_event(action="decompose", user=NOBODY, status_code=200, latency_ms=30_000)

    stats = store.stats()

    assert stats["event_count_30d"] == 33
    assert stats["average_latency_ms_30d"] == recency_weighted_average([30_000] * 3 + [100] * 30)
    assert stats["average_latency_ms_30d"] > round((3 * 30_000 + 30 * 100) / 33)  # above the plain mean
