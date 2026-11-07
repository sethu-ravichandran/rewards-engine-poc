"""Offers: instant discounts that make Checkout (online) differ from Card/Tap, outside the engine."""
import json

import pytest

from scruge.models import Channel, Transaction
from scruge.offers import OfferBook, OfferError
from tests.conftest import body

W = ["amazon_icici", "hdfc_swiggy", "hdfc_infinia", "sbi_cashback"]


def req(amount, merchant, mcc, channel, **kw):
    b = body(amount=amount, mcc=mcc, channel=channel, merchant=merchant, wallet=W, **kw)
    return b


def test_offers_are_opt_in_and_default_behaviour_is_unchanged(make_client):
    c = make_client()
    base = c.post("/api/optimize", json=req(6000, "amazon", "5311", "online")).json()
    assert base["best"]["offer"] is None and base["best"]["total_paise"] == base["best"]["net_paise"]
    assert base["best"]["card_id"] == "amazon_icici"           # 5% on Amazon wins on rewards alone


def test_checkout_differs_from_card_and_tap_because_offers_are_online_only(make_client):
    c = make_client()
    online = c.post("/api/optimize", json=req(6000, "amazon", "5311", "online", include_offers=True)).json()
    card = c.post("/api/optimize", json=req(6000, "amazon", "5311", "card", include_offers=True)).json()
    tap = c.post("/api/optimize", json=req(6000, "amazon", "5311", "tap", include_offers=True)).json()
    # HDFC cards get 10% off on Amazon above Rs 5,000, but only on the Checkout agent. Swiggy HDFC also earns
    # 5% on online department stores, so it beats Infinia once the offer is added.
    assert online["best"]["card_id"] == "hdfc_swiggy" and online["best"]["offer"]["id"] == "hdfc_amazon_10"
    assert card["best"]["card_id"] == tap["best"]["card_id"] == "amazon_icici"
    assert online["best"]["offer_paise"] == 60_000
    assert online["best"]["total_paise"] == online["best"]["net_paise"] + 60_000


def test_minimum_order_and_cap_are_enforced(make_client):
    c = make_client()
    small = c.post("/api/optimize", json=req(2500, "amazon", "5311", "online", include_offers=True)).json()
    assert all(e["offer"] is None for e in small["ranked"])                    # below the Rs 5,000 minimum
    big = c.post("/api/optimize", json=req(20_000, "amazon", "5311", "online", include_offers=True)).json()
    infinia = next(e for e in big["ranked"] if e["card_id"] == "hdfc_infinia")
    assert infinia["offer_paise"] == 100_000                                    # 10% of 20,000 = 2,000, capped at 1,000


def test_voucher_applies_to_any_card_and_shows_in_why(make_client):
    c = make_client()
    r = c.post("/api/optimize", json=req(1000, "swiggy", "5814", "online", include_offers=True)).json()
    by = {e["card_id"]: e for e in r["ranked"] if e["eligible"]}
    assert by["hdfc_swiggy"]["offer"] is None                                    # its own 10% beats the 6% voucher: the voucher is dropped
    assert all(e["offer"]["id"] == "swiggy_voucher_6" for cid, e in by.items() if cid != "hdfc_swiggy")
    assert r["best"]["card_id"] == "hdfc_swiggy" and r["best"]["total_paise"] == 10_000
    only = c.post("/api/optimize", json={**req(1000, "swiggy", "5814", "online", include_offers=True), "wallet": ["amazon_icici"]}).json()["best"]
    assert only["offer_paise"] == 6_000 and only["reward_paise"] == 0 and only["total_paise"] == 6_000      # a voucher purchase earns no card reward
    assert any(w.startswith("Offer: Swiggy voucher") for w in only["why"]) and any("earns no reward" in w for w in only["why"])
    off = c.post("/api/optimize", json=req(1000, "swiggy", "5814", "card", include_offers=True)).json()
    assert off["best"]["offer"] is None                                          # not online: no voucher


def test_commit_reports_offer_and_left_on_table_includes_offers(make_client):
    c = make_client()
    b = req(6000, "amazon", "5311", "online", include_offers=True); b["card_id"] = "amazon_icici"
    r = c.post("/api/commit", json=b).json()
    assert r["best"]["card_id"] == "hdfc_swiggy"
    assert r["left_on_table_paise"] == r["best"]["total_paise"] - r["used"]["total_paise"] > 0


def test_offers_on_and_off_give_different_answers(make_client):
    c = make_client()
    on = c.post("/api/optimize", json=req(6000, "amazon", "5311", "online", include_offers=True)).json()
    off = c.post("/api/optimize", json=req(6000, "amazon", "5311", "online")).json()
    on2 = c.post("/api/optimize", json=req(6000, "amazon", "5311", "online", include_offers=True)).json()
    assert on["best"]["card_id"] != off["best"]["card_id"] and on == on2


def test_offers_endpoint_lists_offers_with_provenance(make_client):
    r = make_client().get("/api/offers").json()
    assert r["version"] and {o["id"] for o in r["offers"]} >= {"swiggy_voucher_6", "hdfc_amazon_10"}
    assert all(o["confidence"] == "illustrative" for o in r["offers"])


def test_ties_between_offers_are_deterministic():
    data = {"offers": [
        {"id": "a", "merchants": ["m"], "cards": ["*"], "channels": ["online"], "discount_bps": 500},
        {"id": "b", "merchants": ["m"], "cards": ["*"], "channels": ["online"], "discount_bps": 500}]}
    book = OfferBook.from_data(data)
    t = Transaction(100_000, "5311", Channel.ONLINE, "m")
    assert all(book.best_for("x", t).offer.id == "a" for _ in range(20))


@pytest.mark.parametrize("bad", [
    {"offers": "x"}, {"offers": [{"id": ""}]},
    {"offers": [{"id": "a", "merchants": [], "cards": ["*"], "channels": ["online"], "discount_bps": 5}]},
    {"offers": [{"id": "a", "merchants": ["m"], "cards": ["*"], "channels": ["carrier-pigeon"], "discount_bps": 5}]},
    {"offers": [{"id": "a", "merchants": ["m"], "cards": ["*"], "channels": ["online"], "discount_bps": 0}]},
    {"offers": [{"id": "a", "merchants": ["m"], "cards": ["*"], "channels": ["online"], "discount_bps": 5, "cap_paise": -1}]},
])
def test_invalid_offers_are_rejected(bad):
    with pytest.raises(OfferError):
        OfferBook.from_data(bad)


def test_invalid_offers_file_keeps_old_book_on_reload(make_client, tmp_path):
    p = tmp_path / "offers.json"
    p.write_text(json.dumps({"offers": [{"id": "x", "merchants": ["m"], "cards": ["*"], "channels": ["online"], "discount_bps": 100}]}))
    c = make_client(offers_path=str(p), admin_token="t")
    v1 = c.get("/api/offers").json()["version"]
    p.write_text("{not json")
    r = c.post("/api/admin/reload", headers={"X-Admin-Token": "t"})
    assert r.status_code == 422 and c.get("/api/offers").json()["version"] == v1


# ---- per-user, per-cycle offer limits ----
def swiggy_req(**kw):
    return req(1500, "swiggy", "5814", "online", include_offers=True, **kw)


def test_offer_stops_paying_when_the_cycle_limit_is_used_up(make_client):
    c = make_client()
    swiggy_req = lambda **kw: {**globals()["swiggy_req"](**kw), "wallet": ["amazon_icici"]}      # noqa: E731  (a card whose own reward the voucher beats)
    fresh = c.post("/api/optimize", json=swiggy_req()).json()["best"]
    assert fresh["offer"]["id"] == "swiggy_voucher_6" and fresh["offer_paise"] == 9_000                       # 6% of Rs 1,500
    part = c.post("/api/optimize", json=swiggy_req(offer_usage_paise={"swiggy_voucher_6": 55_000})).json()["best"]
    assert part["offer_paise"] == 5_000                                                                       # only Rs 50 of the Rs 600 limit left
    gone = c.post("/api/optimize", json=swiggy_req(offer_usage_paise={"swiggy_voucher_6": 60_000})).json()["best"]
    assert gone["offer"] is None and gone["total_paise"] == gone["net_paise"] == 3_000          # back to the card's own 2%


def test_persistent_commits_track_offer_usage_per_user_per_cycle(make_client):
    c = make_client(); uid = "offer-limit-user"
    got = []
    for i in range(8):
        r = c.post("/api/commit", json={**swiggy_req(), "wallet": ["amazon_icici"], "user_id": uid, "txn_ref": f"voucher-{i:03d}"}).json()
        got.append(r["used"]["offer_paise"])
    assert got == [9_000] * 6 + [6_000, 0]                  # Rs 600 a cycle: six full vouchers, a partial one, then nothing
    st = c.get(f"/api/users/{uid}/state").json()
    assert st["totals"]["offer_paise"] == 60_000
    other_cycle = c.post("/api/commit", json={**swiggy_req(), "wallet": ["amazon_icici"], "user_id": uid, "txn_ref": "voucher-new", "cycle": "2026-08"}).json()
    assert other_cycle["used"]["offer_paise"] == 9_000      # a new statement cycle: the limit starts over


def test_the_planner_knows_an_offer_will_run_out():
    from scruge.catalog import load_cards
    from scruge.models import Channel, Transaction
    from scruge.planner import plan
    cards = [load_cards()["amazon_icici"]]       # a card whose own reward the voucher beats
    book = OfferBook.load()
    txns = [Transaction(150_000, "5814", Channel.ONLINE, "swiggy")] * 3
    full = plan(cards, txns, {}, book)
    used = plan(cards, txns, {}, book, offer_usage={"swiggy_voucher_6": 50_000})
    # three Rs 1,500 orders would each get a Rs 90 voucher (Rs 270); with Rs 500 of the Rs 600 cycle limit already used,
    # only Rs 100 is left: Rs 90 for the first order, Rs 10 for the second, nothing for the third
    assert full.total_paise == 27_000 and used.total_paise == 9_000 + 3_000 + 3_000     # 2nd order: the Rs 10 left is worth less than the card's own Rs 30, so the voucher is dropped
