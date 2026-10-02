"""本月 LINE 用量快照：什麼時候該再問 LINE、問不到時留住舊數字（見 core/quota.py）。"""

from __future__ import annotations

import datetime as dt

from church_bot.core.history import History
from church_bot.core.quota import FRESH, SETTLE, QuotaSnapshot, load_quota, save_quota
from church_bot.messengers.base import Quota

NOW = dt.datetime(2026, 10, 2, 20, 0, tzinfo=dt.timezone.utc)


def test_never_checked_is_due_and_says_so():
    snapshot = QuotaSnapshot()
    assert snapshot.due(NOW) and not snapshot.known
    assert snapshot.age_text(NOW) == "還沒查到"


def test_fresh_snapshot_is_reused_then_expires():
    snapshot = QuotaSnapshot().updated(Quota(limit=200, used=37), NOW)
    assert snapshot.used == 37 and snapshot.remaining == 163
    assert not snapshot.due(NOW + FRESH - dt.timedelta(minutes=1))
    assert snapshot.due(NOW + FRESH)
    assert "剛剛更新" in snapshot.status_text(NOW)


def test_push_schedules_a_recheck_and_warns_the_number_is_behind():
    snapshot = QuotaSnapshot().updated(Quota(limit=200, used=37), NOW).dirty(NOW)
    assert snapshot.pending and not snapshot.due(NOW)
    assert snapshot.due(NOW + SETTLE)
    assert "LINE 的統計" in snapshot.status_text(NOW)
    assert not snapshot.updated(Quota(limit=200, used=49), NOW + SETTLE).pending


def test_failure_keeps_the_old_number_and_explains_why():
    snapshot = QuotaSnapshot().updated(Quota(limit=200, used=37), NOW).failed("連不上 LINE", NOW)
    assert snapshot.used == 37 and snapshot.checked_at == NOW
    assert "連不上 LINE" in snapshot.status_text(NOW + dt.timedelta(minutes=1))
    assert not snapshot.due(NOW) and snapshot.due(NOW + SETTLE)


def test_age_text_counts_up(paths):
    snapshot = QuotaSnapshot().updated(Quota(limit=None, used=5), NOW)
    assert snapshot.age_text(NOW + dt.timedelta(minutes=3)) == "3 分鐘前更新"
    assert snapshot.age_text(NOW + dt.timedelta(hours=2)) == "2 小時前更新"
    assert snapshot.age_text(NOW + dt.timedelta(days=2)).endswith("更新")
    assert snapshot.describe() == "本月已用 5 則（無上限方案）"


def test_snapshot_survives_a_round_trip_through_the_database(paths):
    history = History(paths.db_file)
    saved = QuotaSnapshot().updated(Quota(limit=200, used=37), NOW).dirty(NOW)
    save_quota(history, saved)
    assert load_quota(History(paths.db_file)) == saved


def test_broken_or_missing_state_reads_as_unknown(paths):
    history = History(paths.db_file)
    assert load_quota(history) == QuotaSnapshot()
    history.set_state("line_quota", {"used": "x", "limit": "none", "checked_at": "壞了"})
    assert load_quota(history) == QuotaSnapshot()
