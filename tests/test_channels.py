"""Channel rules and guidance: Tap and Card are different agents, impossible payments are said to be impossible,
and a payment that loses money comes with advice."""
from scruge.catalog import load_cards
from scruge.channels import TAP_LIMIT_PAISE, block_reason, guidance
from scruge.models import Channel, Transaction
from tests.conftest import body

W = ["amazon_icici", "hdfc_swiggy", "hdfc_infinia", "sbi_cashback", "tata_neu_rupay"]
CARDS = load_cards()


def req(amount_paise, merchant, mcc, channel, **kw):
    b = body(mcc=mcc, channel=channel, merchant=merchant, wallet=W, **kw)
    b["transaction"].pop("amount"); b["transaction"]["amount_paise"] = amount_paise
    return b


def T(paise, mcc, channel, merchant=None):
    return Transaction(paise, mcc, Channel(channel), merchant)


# ---- the rules themselves ----
def test_online_only_merchants_cannot_be_tapped_or_swiped():
    assert "online-only" in block_reason(T(100_000, "5311", "tap", "amazon"), "Amazon")
    assert "Amazon" in block_reason(T(100_000, "5311", "card", "amazon"), "Amazon")
    assert block_reason(T(100_000, "5311", "online", "amazon")) is None
    assert block_reason(T(100_000, "5311", "upi", "amazon")) is None           # UPI works anywhere
    assert block_reason(T(100_000, "5411", "tap", "dmart")) is None            # a real store


def test_contactless_limit_boundary():
    assert block_reason(T(TAP_LIMIT_PAISE, "5411", "tap", "dmart")) is None
    assert "need a PIN" in block_reason(T(TAP_LIMIT_PAISE + 1, "5411", "tap", "dmart"))
    assert block_reason(T(TAP_LIMIT_PAISE + 1, "5411", "card", "dmart")) is None      # Card is the PIN path


# ---- through the API ----
def test_rules_are_opt_in_so_existing_clients_are_unchanged(make_client):
    c = make_client()
    r = c.post("/api/optimize", json=req(600_000, "amazon", "5311", "tap")).json()
    assert r["best"] is not None and "advice" not in r                           # no flag: engine answer as before


def test_amazon_by_tap_is_unavailable_and_says_why(make_client):
    c = make_client()
    r = c.post("/api/optimize", json=req(600_000, "amazon", "5311", "tap", channel_rules=True)).json()
    assert r["best"] is None and not any(e["eligible"] for e in r["ranked"])
    assert "online-only" in r["advice"][0] and "Checkout" in r["advice"][0]
    assert "online-only" in r["ranked"][0]["why"][-1]


def test_tap_above_the_pin_limit_points_to_card(make_client):
    c = make_client()
    over = c.post("/api/optimize", json=req(600_000, "dmart", "5411", "tap", channel_rules=True)).json()
    assert over["best"] is None and "Card" in over["advice"][0]
    card = c.post("/api/optimize", json=req(600_000, "dmart", "5411", "card", channel_rules=True)).json()
    assert card["best"] is not None and "advice" not in card


def test_tap_differs_from_card_at_a_store_because_of_in_store_tap_offers(make_client):
    c = make_client()
    tap = c.post("/api/optimize", json=req(400_000, "dmart", "5411", "tap", include_offers=True, channel_rules=True)).json()
    card = c.post("/api/optimize", json=req(400_000, "dmart", "5411", "card", include_offers=True, channel_rules=True)).json()
    assert tap["best"]["offer"]["id"] == "dmart_tap_hdfc_5" and tap["best"]["offer_paise"] == 20_000          # 5% of Rs 4,000
    assert card["best"]["offer"] is None
    # the same card can win both, but Tap is worth Rs 200 more at the counter: that is the difference between the two agents
    assert tap["best"]["card_id"] == card["best"]["card_id"] == "hdfc_infinia"
    assert tap["best"]["total_paise"] == card["best"]["total_paise"] + 20_000


def test_commit_refuses_an_impossible_payment_with_the_reason(make_client):
    c = make_client()
    r = c.post("/api/commit", json={**req(600_000, "amazon", "5311", "tap", channel_rules=True)})
    assert r.status_code == 422 and "online-only" in r.text


def test_meta_marks_online_only_merchants_and_the_tap_limit(make_client):
    m = make_client().get("/api/meta").json()
    ms = {x["id"]: x for x in m["merchants"]}
    assert ms["amazon"]["online_only"] and ms["swiggy"]["online_only"] and not ms["dmart"]["online_only"] and not ms["indian_oil"]["online_only"]
    assert m["tap_limit_paise"] == 500_000


def test_rules_on_and_off_give_different_answers(make_client):
    c = make_client()
    on = c.post("/api/optimize", json=req(600_000, "amazon", "5311", "tap", channel_rules=True)).json()
    off = c.post("/api/optimize", json=req(600_000, "amazon", "5311", "tap")).json()
    assert on["best"] is None and off["best"] is not None


def test_planner_never_plans_an_impossible_payment(make_client):
    c = make_client()
    txs = [{"amount_paise": 600_000, "mcc": "5311", "channel": "tap", "merchant": "amazon"},
           {"amount_paise": 100_000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}]
    r = c.post("/api/plan", json={"transactions": txs, "wallet": W}).json()
    assert r["steps"][0]["card_id"] is None and r["steps"][1]["card_id"] is not None      # the impossible one is skipped, not forced onto a card
    forced = c.post("/api/plan", json={"transactions": txs, "wallet": W, "channel_rules": False}).json()
    assert forced["steps"][0]["card_id"] is not None


# ---- guidance for payments that lose money ----
def test_petrol_above_the_waiver_band_says_to_split(make_client):
    c = make_client()
    r = c.post("/api/optimize", json=req(600_000, "indian_oil", "5541", "card", channel_rules=True)).json()
    assert r["best"]["total_paise"] < 0
    tip = r["advice"][0]
    assert "Split ₹6,000 into 2 payments of about ₹3,000" in tip and "waived between" in tip


def test_petrol_below_the_band_is_honest_that_the_fee_is_tiny():
    """A Rs 300 fill loses Rs 3. Nobody should be told to buy more petrol to dodge that."""
    tip = guidance(list(CARDS.values()), T(30_000, "5541", "card", "indian_oil"), -300)[0]
    assert "only ₹3.00" in tip and "Pay at least" not in tip and "small top-up" in tip


def test_the_advice_names_the_card_the_screen_recommends(make_client):
    c = make_client()
    r = c.post("/api/optimize", json=req(600_000, "indian_oil", "5541", "card", channel_rules=True)).json()
    assert r["best"]["card"] in r["advice"][0]
    only_swiggy = c.post("/api/optimize", json={**req(600_000, "indian_oil", "5541", "card", channel_rules=True), "wallet": ["amazon_icici", "hdfc_swiggy"]}).json()
    assert "Split ₹6,000" in only_swiggy["advice"][0]


def test_rent_says_to_use_a_bank_transfer(make_client):
    c = make_client()
    r = c.post("/api/optimize", json=req(2_500_000, "rent", "6513", "online", channel_rules=True)).json()
    tip = r["advice"][0]
    assert r["best"]["total_paise"] < 0 and "bank account" in tip and "1%" in tip
    assert "credit card on UPI is still a card payment" in tip          # UPI from the credit card is NOT a way out: say so


def test_no_advice_when_the_payment_is_fine():
    assert guidance(list(CARDS.values()), T(100_000, "5814", "online", "swiggy"), 5_000) == []
    assert guidance(list(CARDS.values()), T(100_000, "5814", "online", "swiggy"), None) == []
