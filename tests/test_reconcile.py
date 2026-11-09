"""Reward reconciliation: predicted vs credited, with a diagnosis for every mismatch."""
from datetime import datetime

from rewards_engine.catalog import load_cards
from rewards_engine.reconcile import Credit, Expected, diagnose, reconcile, tolerance
from tests.conftest import body

CARDS = load_cards()
T0 = "2026-10-01T10:00:00+00:00"


def exp(ref, card="amazon_icici", amount=250_000, expected=12_500, rule="amazon_5pct", merchant="amazon", at=T0):
    return Expected(ref, card, amount, expected, at, rule, merchant)


def run(expected, credits, now="2026-10-10T00:00:00+00:00", days=45):
    return reconcile(expected, credits, CARDS, datetime.fromisoformat(now), days)


def test_exact_and_within_tolerance_match():
    r = run([exp("a"), exp("b")], [Credit("a", 12_500), Credit("b", 12_450)])
    assert r["counts"]["matched"] == 2 and r["shortfall_paise"] == 0
    assert tolerance(12_500) == 250 and tolerance(1_000) == 100           # Rs 1 floor, otherwise 2%


def test_short_credit_diagnosed_as_base_rate_with_dispute_note():
    r = run([exp("a")], [Credit("a", 2_500)])                             # 1% base instead of 5%
    it = r["items"][0]
    assert it["status"] == "short" and it["diff_paise"] == -10_000 and r["shortfall_paise"] == 10_000
    assert it["diagnosis"]["cause"] == "base_rate" and "base rate (1.00%)" in it["diagnosis"]["detail"]
    n = it["dispute_note"]
    assert "transaction a" in n and "₹125.00" in n and "₹25.00" in n and "₹100.00" in n and "amazon_5pct" in n


def test_over_credit_flags_stale_rules_without_a_dispute_note():
    r = run([exp("a", expected=2_500)], [Credit("a", 12_500)])
    it = r["items"][0]
    assert it["status"] == "over" and "dispute_note" not in it and r["shortfall_paise"] == 0
    assert it["diagnosis"]["cause"] == "other_rule"                       # matches the 5% rule: our expectation was the stale one


def test_zero_credit_and_unexplained():
    z = run([exp("a")], [Credit("a", 0)])["items"][0]
    assert z["diagnosis"]["cause"] == "no_reward"
    u = run([exp("a")], [Credit("a", 7_777)])["items"][0]
    assert u["diagnosis"]["cause"] == "unexplained" and "disputing" in u["diagnosis"]["detail"]


def test_cap_reached_hypothesis():
    e = Expected("a", "hdfc_swiggy", 1_000_000, 100_000, T0, "swiggy_10pct", "swiggy")   # expected 10% = Rs 1,000
    d = diagnose(CARDS["hdfc_swiggy"], e, 40_000)       # between base (Rs 100) and expected, not a known rate
    assert d["cause"] == "cap_reached" and "swiggy_10pct" in d["detail"]


def test_pending_then_overdue_by_settlement_window():
    e = [exp("a")]
    assert run(e, [], now="2026-10-20T00:00:00+00:00")["items"][0]["status"] == "pending"
    late = run(e, [], now="2026-12-01T00:00:00+00:00")
    assert late["items"][0]["status"] == "overdue" and late["shortfall_paise"] == 12_500
    assert late["items"][0]["diagnosis"]["cause"] == "not_credited" and "dispute_note" in late["items"][0]


def test_totals_and_counts():
    r = run([exp("a"), exp("b"), exp("c")], [Credit("a", 12_500), Credit("b", 2_500)])
    assert r["counts"] == {"matched": 1, "short": 1, "over": 0, "pending": 1, "overdue": 0}
    assert r["expected_paise"] == 37_500 and r["credited_paise"] == 15_000 and r["shortfall_paise"] == 10_000


def test_stateless_endpoint(make_client):
    c = make_client()
    payload = {"expected": [{"txn_ref": "a", "card_id": "amazon_icici", "amount_paise": 250_000, "expected_paise": 12_500,
                             "created_at": T0, "rule_id": "amazon_5pct", "merchant": "amazon"}],
               "credits": [{"txn_ref": "a", "actual_paise": 2_500}], "as_of": "2026-10-10T00:00:00Z"}
    r = c.post("/api/reconcile", json=payload).json()
    assert r["items"][0]["diagnosis"]["cause"] == "base_rate" and r["shortfall_paise"] == 10_000
    assert c.post("/api/reconcile", json={**payload, "as_of": "yesterday-ish"}).status_code == 422


def test_end_to_end_user_flow_commit_credit_reconcile(make_client):
    """Pay (ledger row with the predicted reward), the issuer credits one payment short, reconcile finds it."""
    c = make_client()
    uid = "recon-user-01"
    for ref, amt in (("pay-0001", 2500), ("pay-0002", 1800)):
        r = c.post("/api/commit", json={**body(amount=amt, mcc="5311", channel="online", merchant="amazon", wallet=["amazon_icici"]), "user_id": uid, "txn_ref": ref}).json()
        assert r["persisted"] and r["used"]["reward_paise"] == amt * 5
    state = c.get(f"/api/users/{uid}/state").json()
    assert [row["expected_paise"] for row in state["ledger"]] == [9_000, 12_500]        # newest first
    out = c.post(f"/api/users/{uid}/credits", json={"credits": [{"txn_ref": "pay-0001", "actual_paise": 12_500},
                                                                {"txn_ref": "pay-0002", "actual_paise": 1_800}]}).json()
    assert out["recorded"] == 2
    items = {i["txn_ref"]: i for i in out["report"]["items"]}
    assert items["pay-0001"]["status"] == "matched"
    assert items["pay-0002"]["status"] == "short" and items["pay-0002"]["diagnosis"]["cause"] == "base_rate"
    assert out["report"]["shortfall_paise"] == 7_200
    assert c.get(f"/api/users/{uid}/reconcile").json()["shortfall_paise"] == 7_200


def test_cap_diagnosis_names_the_rule_that_was_actually_applied():
    """Regression: a short credit on Swiggy HDFC's online_cat_5pct reward was blamed on the unrelated swiggy_10pct cap."""
    e = Expected("t", "hdfc_swiggy", 600_000, 30_000, T0, "online_cat_5pct", "amazon")       # 5% on Rs 6,000 = Rs 300
    d = diagnose(CARDS["hdfc_swiggy"], e, 14_000)                                            # credited Rs 140: between base (Rs 60) and Rs 300
    assert d["cause"] == "cap_reached" and d["rule"] == "online_cat_5pct" and "swiggy_10pct" not in d["detail"]
    assert "online_5" in d["detail"] and "₹1,500" in d["detail"]


def test_an_uncapped_rule_is_never_blamed_on_a_cap():
    amazon = Expected("t", "amazon_icici", 250_000, 12_500, T0, "amazon_5pct", "amazon")     # amazon_5pct has no cap
    assert diagnose(CARDS["amazon_icici"], amazon, 7_000)["cause"] == "unexplained"
    swiggy_base = Expected("t", "hdfc_swiggy", 100_000, 1_000, T0, None, "x")                # expected at base rate: no rule, no cap story
    assert diagnose(CARDS["hdfc_swiggy"], swiggy_base, 500)["cause"] == "unexplained"


def test_an_over_credit_is_never_told_to_dispute():
    over_known = diagnose(CARDS["amazon_icici"], Expected("t", "amazon_icici", 250_000, 2_500, T0, None, "x"), 12_500)     # paid the 5% rule we did not expect
    assert over_known["cause"] == "other_rule" and "nothing to dispute" in over_known["detail"] and "Worth disputing" not in over_known["detail"]
    over_unknown = diagnose(CARDS["amazon_icici"], Expected("t", "amazon_icici", 250_000, 2_500, T0, None, "x"), 7_777)
    assert over_unknown["cause"] == "over_credit" and "nothing to dispute" in over_unknown["detail"] and "Worth disputing" not in over_unknown["detail"]
    assert "disputing" in diagnose(CARDS["amazon_icici"], Expected("t", "amazon_icici", 250_000, 12_500, T0, "amazon_5pct", "x"), 7_777)["detail"]   # a SHORT unexplained credit still is
