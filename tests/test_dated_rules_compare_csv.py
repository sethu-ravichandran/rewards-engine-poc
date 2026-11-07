"""Dated rules, route and price comparison, CSV statement import, and boundary values on every paise field."""
import json

import pytest

from scruge.catalog import CatalogError, card_from_dict, card_on, validate_catalog
from tests.conftest import body

W = ["sbi_cashback", "amazon_icici", "hdfc_swiggy", "hdfc_infinia", "tata_neu_rupay"]


# ---- effective_from / effective_to ----
def dated_card():
    return {"id": "x", "name": "X", "network": "visa", "base_rate_bps": 100, "rules": [
        {"id": "old", "rate_bps": 500, "merchants": ["amazon"], "effective_to": "2026-12-01"},
        {"id": "new", "rate_bps": 300, "merchants": ["amazon"], "effective_from": "2026-12-01"}]}


def test_card_on_picks_the_rules_in_force_on_that_day():
    c = card_from_dict(dated_card())
    assert [r.id for r in card_on(c, "2026-11-30").rules] == ["old"]
    assert [r.id for r in card_on(c, "2026-12-01").rules] == ["new"]      # from is inclusive, to is exclusive
    undated = card_from_dict({**dated_card(), "rules": [{"id": "a", "rate_bps": 200}]})
    assert card_on(undated, "2026-12-01") is undated


def test_same_rule_id_twice_only_as_non_overlapping_dated_versions():
    d = {"cards": [{**dated_card(), "rules": [{"id": "r", "rate_bps": 500, "effective_to": "2026-12-01"},
                                              {"id": "r", "rate_bps": 300, "effective_from": "2026-12-01"}]}]}
    validate_catalog(d)
    d["cards"][0]["rules"][1]["effective_from"] = "2026-11-01"             # overlaps
    with pytest.raises(CatalogError, match="duplicate rule id"):
        validate_catalog(d)
    d["cards"][0]["rules"] = [{"id": "r", "rate_bps": 5}, {"id": "r", "rate_bps": 6}]
    with pytest.raises(CatalogError, match="duplicate rule id"):
        validate_catalog(d)
    d["cards"][0]["rules"] = [{"id": "r", "rate_bps": 5, "effective_from": "2026-12-01", "effective_to": "2026-01-01"}]
    with pytest.raises(CatalogError, match="before"):
        validate_catalog(d)


def test_api_prices_a_payment_with_the_rules_of_its_date(make_client, rules_file):
    data = json.loads(rules_file.read_text())
    data["cards"].append(dated_card())
    rules_file.write_text(json.dumps(data))
    c = make_client(admin_token="t")
    c.post("/api/admin/reload", headers={"X-Admin-Token": "t"})

    def reward(day):
        b = body(mcc="5311", channel="online", merchant="amazon", wallet=["x"], amount=1000)
        b["transaction"]["on"] = day
        return c.post("/api/optimize", json=b).json()["best"]["reward_paise"]
    assert reward("2026-11-30") == 5000 and reward("2026-12-01") == 3000      # 5% before the change, 3% after
    bad = body(mcc="5311", channel="online", wallet=["x"]); bad["transaction"]["on"] = "2026-02-30"
    assert c.post("/api/optimize", json=bad).status_code == 422


# ---- comparison ----
def test_routes_say_which_way_to_pay_wins_and_why_others_cannot(make_client):
    c = make_client()
    r = c.post("/api/compare/routes", json={"transaction": {"amount_paise": 500000, "mcc": "5411", "channel": "upi", "merchant": "bigbasket"}, "wallet": W}).json()
    by = {x["channel"]: x for x in r["routes"]}
    assert by["card"]["available"] is False and "online-only" in by["card"]["reason"]
    assert r["winner"] == "online" and by["online"]["winner"] and by["upi"]["behind_paise"] == by["online"]["total_paise"] - by["upi"]["total_paise"]
    assert by["upi"]["offer_paise"] == 10_000            # the 3% UPI offer, capped at Rs 100


def test_upi_can_win_through_a_bank_account_with_no_card(make_client):
    c = make_client()
    r = c.post("/api/compare/routes", json={"transaction": {"amount_paise": 500000, "mcc": "5411", "channel": "upi", "merchant": "bigbasket"}, "wallet": ["sbi_cashback"]}).json()
    up = next(x for x in r["routes"] if x["channel"] == "upi")
    assert up["name"] == "Bank account (UPI)" and up["card_id"] is None and up["total_paise"] == 10_000


def test_cheapest_sticker_is_not_always_cheapest_after_rewards(make_client):
    c = make_client()
    r = c.post("/api/compare/prices", json={"item": "headphones", "wallet": W}).json()
    assert r["differs"] and r["cheapest_sticker"] == "flipkart" and r["listings"][0]["merchant"] != "flipkart"
    assert all(x["effective_paise"] == x["price_paise"] - x["total_paise"] for x in r["listings"])
    assert r["saves_vs_sticker_paise"] > 0
    assert c.post("/api/compare/prices", json={"item": "nope", "wallet": W}).status_code == 404


# ---- CSV statement ----
def commit(c, uid, ref):
    b = body(mcc="5311", channel="online", merchant="amazon", wallet=["amazon_icici"], amount=1000)
    b.update(user_id=uid, txn_ref=ref)
    return c.post("/api/commit", json=b).json()


def test_csv_statement_reconciles_and_lists_bad_rows(make_client):
    c = make_client()
    commit(c, "csv_user_001", "csvref001"); commit(c, "csv_user_001", "csvref002")
    csv_text = "txn_ref,credited_inr,credited_at\ncsvref001,50.00,2026-11-20\ncsvref002,x,2026-11-20\nghost0001,5,\n,1,\n"
    r = c.post("/api/users/csv_user_001/statement", content=csv_text, headers={"content-type": "text/csv"}).json()
    assert r["rows"] == 4 and r["recorded"] == 1
    assert r["unmatched_refs"] == ["ghost0001"]
    assert {x["line"] for x in r["rejected"]} == {3, 5}
    assert c.post("/api/users/csv_user_001/statement", content="a,b\n1,2\n").status_code == 422
    assert c.post("/api/users/csv_user_001/statement", content="").status_code == 422


# ---- boundaries: no integer, however large, may produce a 500 ----
HUGE = [10**19, 10**30, -(10**19), 2**63, 2**63 - 1, 10**13 + 1]


@pytest.mark.parametrize("v", HUGE)
def test_oversized_integers_are_422_on_every_paise_field(make_client, v):
    c = make_client()
    ok = {"transaction": {"amount_paise": 1000, "mcc": "5411", "channel": "card"}, "wallet": ["sbi_cashback"]}
    cases = [
        ("/api/optimize", {**ok, "transaction": {**ok["transaction"], "amount_paise": v}}),
        ("/api/optimize", {**ok, "cycle_usage_paise": {"sbi_cashback": {"b": v}}}),
        ("/api/optimize", {**ok, "offer_usage_paise": {"o": v}}),
        ("/api/commit", {**ok, "user_id": "fuzz_user_001", "txn_ref": "fuzzref001", "transaction": {**ok["transaction"], "amount_paise": v}}),
        ("/api/users/fuzz_user_001/credits", {"credits": [{"txn_ref": "fuzzref001", "actual_paise": v}]}),
        ("/api/reconcile", {"expected": [{"txn_ref": "fuzzref001", "card_id": "sbi_cashback", "amount_paise": v, "expected_paise": 1, "created_at": "2026-11-01T00:00:00+00:00"}], "credits": []}),
        ("/api/reconcile", {"expected": [{"txn_ref": "fuzzref001", "card_id": "sbi_cashback", "amount_paise": 1, "expected_paise": v, "created_at": "2026-11-01T00:00:00+00:00"}], "credits": []}),
        ("/api/plan", {"transactions": [{"amount_paise": v, "mcc": "5411", "channel": "card"}], "wallet": ["sbi_cashback"]}),
    ]
    for url, payload in cases:
        code = c.post(url, json=payload).status_code
        assert code == 422, (url, v, code)


# ---- more edge cases ----
def test_legacy_float_usage_never_500s(make_client):
    c = make_client()
    for v in (1e300, -1e300, 1e30):
        b = {"transaction": {"amount_paise": 100000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}, "wallet": ["sbi_cashback"], "cycle_usage": {"sbi_cashback": {"x": v}}}
        assert c.post("/api/optimize", json=b).status_code == 422


def test_diff_sees_dated_versions_and_scheduled_changes():
    import copy

    from scruge import rules
    live = json.loads(open("data/cards.json").read())
    new = copy.deepcopy(live)
    sw = next(k for k in new["cards"] if k["id"] == "hdfc_swiggy")
    r = next(x for x in sw["rules"] if x["id"] == "swiggy_10pct")
    r["effective_from"] = "2099-01-01"                              # the 10% rule would stop applying today
    d = rules.diff(live, new)
    assert d["changed"] and any("live today and would stop applying" in x for x in d["review_required"])
    # a scheduled 10% -> 1% change is shown as scheduled, not as an immediate rewrite of the live rule
    new = copy.deepcopy(live)
    sw = next(k for k in new["cards"] if k["id"] == "hdfc_swiggy")
    i = next(i for i, x in enumerate(sw["rules"]) if x["id"] == "swiggy_10pct")
    cur = sw["rules"][i]; cur["effective_to"] = "2099-01-01"
    sw["rules"].insert(i + 1, {**cur, "effective_from": "2099-01-01", "effective_to": None, "rate_bps": 100})
    d = rules.diff(live, new)
    assert any("scheduled to move 10.00% -> 1.00% on 2099-01-01" in x for x in d["review_required"])
    assert not any("rate 10.00% -> 1.00%" in x for x in d["changed"]["hdfc_swiggy"])


def test_impossible_dates_are_refused_in_rules():
    for bad in ("2026-13-45", "2026-01-01\n", "2026-1-1", "tomorrow"):
        d = {"cards": [{**dated_card(), "rules": [{"id": "r", "rate_bps": 5, "effective_from": bad}]}]}
        with pytest.raises(CatalogError):
            validate_catalog(d)


def test_on_is_checked_and_applied_on_batch_and_plan(make_client):
    c = make_client()
    tx = {"amount_paise": 1000, "mcc": "5814", "channel": "online", "on": "2026-02-31"}
    assert c.post("/api/optimize/batch", json={"transactions": [tx], "wallet": ["sbi_cashback"]}).status_code == 422
    assert c.post("/api/plan", json={"transactions": [tx], "wallet": ["sbi_cashback"]}).status_code == 422
    mixed = [{**tx, "on": "2026-11-01"}, {**tx, "on": "2026-11-02"}]
    r = c.post("/api/plan", json={"transactions": mixed, "wallet": ["sbi_cashback"]})
    assert r.status_code == 422 and "share one date" in r.text


def test_commit_with_on_lands_in_that_payments_cycle(make_client):
    c = make_client()
    b = body(mcc="5311", channel="online", merchant="amazon", wallet=["amazon_icici"], amount=1000)
    b["transaction"]["on"] = "2026-03-15"; b.update(user_id="backdate_user1", txn_ref="backdate0001")
    r = c.post("/api/commit", json=b).json()
    assert r["cycle"] == "2026-03"
    row = c.get("/api/users/backdate_user1/state", params={"cycle": "2026-03"}).json()["ledger"][0]
    assert row["created_at"].startswith("2026-03-15")


def test_csv_repeated_refs_are_refused_not_last_wins(make_client):
    c = make_client()
    commit(c, "dup_user_0001", "dupref0001")
    text = "txn_ref,credited_paise\ndupref0001,10000\ndupref0001,3\n"
    r = c.post("/api/users/dup_user_0001/statement", content=text).json()
    assert r["recorded"] == 0 and [x["line"] for x in r["rejected"]] == [2, 3] and "appears 2 times" in r["rejected"][0]["error"]
    assert r["distinct_refs"] == 1
    bad = c.post("/api/users/dup_user_0001/statement", content="txn_ref,credited_paise,credited_at\ndupref0001,5,garbage\n").json()
    assert bad["rejected"][0]["error"].startswith("credited_at")
    assert c.post("/api/users/dup_user_0001/credits", json={"credits": [{"txn_ref": "dupref0001", "actual_paise": 1, "credited_at": "garbage"}]}).status_code == 422


def test_a_voucher_replaces_the_card_reward_and_uses_no_cap(make_client):
    c = make_client()
    b = body(mcc="5814", channel="online", merchant="swiggy", wallet=["amazon_icici"], amount=1500)
    b.update(include_offers=True, user_id="voucher_user_1", txn_ref="voucher00001")
    r = c.post("/api/commit", json=b).json()
    assert r["used"]["offer_paise"] == 9_000 and r["used"]["reward_paise"] == 0
    row = c.get("/api/users/voucher_user_1/state").json()["ledger"][0]
    assert row["expected_paise"] == 0 and row["offer_paise"] == 9_000       # reconciliation will not expect a reward that never comes


def test_upi_offer_is_for_a_bank_account_not_a_credit_card_on_upi(make_client):
    c = make_client()
    r = c.post("/api/optimize", json={"transaction": {"amount_paise": 500000, "mcc": "5411", "channel": "upi", "merchant": "bigbasket"}, "wallet": W, "include_offers": True}).json()
    assert r["best"]["offer"] is None                                        # Tata Neu on UPI gets its own reward, not the bank-account offer
