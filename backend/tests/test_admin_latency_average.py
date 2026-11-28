"""The admin dashboard's average latency follows recent actions.

A plain mean over the last 5000 audit events hardly moves when the service gets slower or faster, so each event weighs
half as much as the one ``LATENCY_HALF_LIFE_ACTIONS`` newer. Fully offline.
"""

from __future__ import annotations

from app.auth import LATENCY_HALF_LIFE_ACTIONS, NOBODY, AuthStore, recency_weighted_average
from increment1_support import BOOTSTRAP


def test_a_value_weighs_half_as_much_as_the_one_a_half_life_newer():
    assert recency_weighted_average([]) == 0
    assert recency_weighted_average([500] * 50) == 500
    assert recency_weighted_average([900, 300], half_life=1) == 700  # weights 1 and 1/2: (900 + 150) / 1.5


def test_the_last_half_life_of_actions_carries_half_the_weight():
    values = [30_000] * LATENCY_HALF_LIFE_ACTIONS + [100] * 4000  # newest first
    assert round(sum(values) / len(values)) < 300  # what a plain mean would show
    assert abs(recency_weighted_average(values) - 15_050) <= 5


def dashboard_average(tmp_path, monkeypatch, latencies_oldest_first):
    for name, value in BOOTSTRAP.items():  # an empty database starts only with a first admin (D-045)
        monkeypatch.setenv(name, value)
    store = AuthStore(db_path=tmp_path / "auth.db", enabled=True, session_secret="x", cookie_secure=False)
    store.init()
    for latency in latencies_oldest_first:
        store.audit_event(action="decompose", user=NOBODY, status_code=200, latency_ms=latency)
    stats = store.stats()
    assert stats["event_count_30d"] == len(latencies_oldest_first)
    return stats["average_latency_ms_30d"]


def test_the_dashboard_figure_follows_the_newest_events(tmp_path, monkeypatch):
    plain_mean = round((3 * 30_000 + 30 * 100) / 33)
    slow_lately = dashboard_average(tmp_path / "a", monkeypatch, [100] * 30 + [30_000] * 3)
    fast_lately = dashboard_average(tmp_path / "b", monkeypatch, [30_000] * 3 + [100] * 30)
    assert slow_lately > plain_mean > fast_lately
