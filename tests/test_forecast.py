"""Forecast the rest of the cycle from a user's own history; plan it without the caller supplying payments."""
from datetime import datetime, timezone

from scruge.forecast import forecast
from tests.conftest import body

NOV10 = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)      # 20 of 30 days remain (2/3 of the cycle)
NOV30 = datetime(2026, 11, 30, 12, tzinfo=timezone.utc)


def row(cycle, merchant="swiggy", mcc="5814", channel="online", amount=100_000):
    return {"cycle": cycle, "merchant": merchant, "mcc": mcc, "channel": channel, "amount_paise": amount}


def history(n_per_cycle, cycles=("2026-08", "2026-09", "2026-10"), **kw):
    return [row(c, **kw) for c in cycles for _ in range(n_per_cycle)]


def test_no_history_means_no_forecast_and_says_so():
    f = forecast([], NOV10)
    assert f.confidence == "none" and not f.transactions and "nothing to forecast" in f.note


def test_completed_cycles_drive_the_rate():
    f = forecast(history(6), NOV10)                          # 6 per cycle, none yet this cycle, 2/3 of the cycle left
    assert f.confidence == "high" and f.cycles_observed == 3
    assert len(f.transactions) == 4                          # round(6 * 20/30) = 4
    assert all(t.amount_paise == 100_000 and t.merchant == "swiggy" for t in f.transactions)
    assert f.patterns[0].basis == "history" and f.patterns[0].per_cycle == 6.0


def test_paying_ahead_of_schedule_is_not_double_counted():
    rows = history(6) + [row("2026-11") for _ in range(5)]    # already 5 of the usual 6 this cycle
    assert len(forecast(rows, NOV10).transactions) == 1       # min(round(6*2/3)=4, round(6-5)=1)
    assert not forecast(history(6) + [row("2026-11") for _ in range(6)], NOV10).transactions


def test_end_of_cycle_expects_nothing_more():
    assert not forecast(history(6), NOV30).transactions       # no days left


def test_confidence_tiers():
    assert forecast(history(2, cycles=("2026-10",)), NOV10).confidence == "medium"
    assert forecast(history(2, cycles=("2026-09", "2026-10")), NOV10).confidence == "medium"
    assert forecast(history(2), NOV10).confidence == "high"


def test_new_user_with_only_this_cycle_is_low_confidence_and_capped():
    rows = [row("2026-11", amount=a) for a in (50_000, 70_000, 90_000)]       # 3 payments by day 10
    f = forecast(rows, NOV10)
    assert f.confidence == "low" and f.cycles_observed == 0
    assert 0 < len(f.transactions) <= 3                       # extrapolated, but never more than done so far
    assert f.transactions[0].amount_paise == 70_000           # median amount
    assert "hint" in f.note


def test_amount_is_the_median_so_one_outlier_does_not_skew_it():
    rows = [row("2026-09", amount=a) for a in (100_000, 110_000, 120_000)] + [row("2026-10", amount=5_000_000), row("2026-10", amount=105_000), row("2026-10", amount=115_000)]
    f = forecast(rows, NOV10)
    assert all(t.amount_paise <= 120_000 for t in f.transactions)


def test_several_patterns_interleave_over_the_remaining_days_and_size_is_capped():
    rows = history(6) + history(3, merchant="amazon", mcc="5311", amount=500_000) + history(9, merchant=None, mcc="5411", channel="tap", amount=20_000)
    f = forecast(rows, NOV10, max_payments=29)
    assert len(f.transactions) == 4 + 2 + 6
    merchants = [t.merchant for t in f.transactions]
    assert len(set(merchants)) == 3 and merchants != sorted(merchants, key=lambda m: str(m))     # interleaved, not grouped
    capped = forecast(rows, NOV10, max_payments=5)
    assert len(capped.transactions) == 5 and sum(t.merchant == "amazon" for t in capped.transactions) == 2   # largest payments kept


def test_forecast_is_deterministic():
    rows = history(5) + history(2, merchant="amazon", mcc="5311")
    assert [(t.amount_paise, t.merchant) for t in forecast(rows, NOV10).transactions] == [(t.amount_paise, t.merchant) for t in forecast(list(reversed(rows)), NOV10).transactions]


# ---- through the API: history in the ledger -> forecast -> plan, with nothing supplied by the caller ----
def seed(c, uid, cycles=("2026-08", "2026-09", "2026-10"), per=4):
    for cy in cycles:
        for i in range(per):
            r = c.post("/api/commit", json={**body(amount=1500, wallet=["hdfc_swiggy", "sbi_cashback", "hdfc_infinia"]), "user_id": uid, "txn_ref": f"seed-{cy}-{i:02d}", "cycle": cy})
            assert r.status_code == 200, r.text


def test_forecast_endpoint_reads_the_users_ledger(make_client):
    c = make_client(); uid = "forecast-user-1"
    assert c.get(f"/api/users/{uid}/forecast?as_of=2026-11-10T12:00:00Z").json()["confidence"] == "none"
    seed(c, uid)
    f = c.get(f"/api/users/{uid}/forecast?as_of=2026-11-10T12:00:00Z").json()
    assert f["confidence"] == "high" and f["cycles_observed"] == 3
    assert len(f["payments"]) == 3 and f["patterns"][0]["merchant"] == "swiggy"     # round(4 * 20/30) = 3
    assert c.get(f"/api/users/{uid}/forecast?as_of=nope").status_code == 422


def test_plan_from_history_needs_no_payments_from_the_caller(make_client):
    c = make_client(); uid = "forecast-user-2"
    seed(c, uid)
    c.put(f"/api/users/{uid}/wallet", json={"cards": ["hdfc_swiggy", "sbi_cashback", "hdfc_infinia"]})
    r = c.post(f"/api/users/{uid}/plan", json={"as_of": "2026-11-10T12:00:00Z"}).json()
    assert r["forecast"]["confidence"] == "high" and r["current_included"] is False
    assert len(r["plan"]["steps"]) == 3 and r["plan"]["gain_paise"] >= 0 and isinstance(r["plan"]["optimal"], bool)
    now = c.post(f"/api/users/{uid}/plan", json={"as_of": "2026-11-10T12:00:00Z", "transaction": {"amount_paise": 150_000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}}).json()
    assert now["current_included"] and len(now["plan"]["steps"]) == 4 and "first_move" in now["plan"]


def test_plan_from_history_uses_stored_cap_usage_and_wallet(make_client):
    c = make_client(); uid = "forecast-user-3"
    seed(c, uid, cycles=("2026-09", "2026-10"))
    for i in range(5):    # burn the Swiggy cap this cycle with a different pattern (tap), so the online history still forecasts
        c.post("/api/commit", json={**body(amount=3000, channel="tap", wallet=["hdfc_swiggy", "hdfc_infinia"]), "user_id": uid, "txn_ref": f"burn-nov-{i:02d}", "cycle": "2026-11"})
    r = c.post(f"/api/users/{uid}/plan", json={"wallet": ["hdfc_swiggy", "hdfc_infinia"], "as_of": "2026-11-10T12:00:00Z"}).json()
    assert all(s["card_id"] == "hdfc_infinia" for s in r["plan"]["steps"])            # cap already used up this cycle


def test_plan_from_history_without_wallet_or_history_is_a_clear_answer(make_client):
    c = make_client(); uid = "forecast-user-4"
    assert c.post(f"/api/users/{uid}/plan", json={}).status_code == 422                # no wallet anywhere
    r = c.post(f"/api/users/{uid}/plan", json={"wallet": ["hdfc_swiggy"]}).json()
    assert r["plan"] is None and r["forecast"]["confidence"] == "none"
    cur = c.post(f"/api/users/{uid}/plan", json={"wallet": ["hdfc_swiggy"], "transaction": {"amount_paise": 100_000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}}).json()
    assert len(cur["plan"]["steps"]) == 1 and cur["forecast"]["confidence"] == "none"   # still advises on the payment itself
