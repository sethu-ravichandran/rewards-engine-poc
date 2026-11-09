"""Cap-aware lookahead: never worse than greedy, proven optimal on small cases, honest about its limits."""
import random

from rewards_engine.catalog import load_cards
from rewards_engine.engine import commit, evaluate
from rewards_engine.models import Card, Channel, CycleState, EarnRule, Network, Transaction
from rewards_engine.offers import OfferBook
from rewards_engine.planner import first_move_advice, plan
from tests.conftest import body

X = Card("x", "Card X", Network.VISA, 100,
         rules=(EarnRule("dining10", 1000, categories=frozenset({"dining"}), cap_paise=100_000, cap_bucket="dx"),))
Y = Card("y", "Card Y", Network.VISA, 400)
DINING = "5812"


def dinner(rupees):
    return Transaction(rupees * 100, DINING, Channel.CARD, None)


def test_lookahead_beats_greedy_when_a_cap_should_be_saved():
    """Rs 5,000 dinner then Rs 10,000 dinner: spending X's cap on the first wastes it (see planner docstring)."""
    p = plan([X, Y], [dinner(5000), dinner(10_000)])
    assert p.greedy_total_paise == 105_000 and p.total_paise == 120_000 and p.gain_paise == 15_000
    assert [s.card_id for s in p.greedy_steps] == ["x", "x"] and [s.card_id for s in p.steps] == ["y", "x"]
    assert p.optimal


def test_first_move_advice_says_hold_the_cap():
    p = plan([X, Y], [dinner(5000), dinner(10_000)])
    a = first_move_advice(p, {"x": X, "y": Y}, {})
    assert a["hold"] and a["card_id"] == "y" and a["greedy_card_id"] == "x" and a["gain_paise"] == 15_000
    assert "Hold Card X" in a["reason"] and "₹150.00" in a["reason"]


def test_when_greedy_is_already_best_it_says_so():
    p = plan([X, Y], [dinner(10_000), dinner(5000)])      # big one first: greedy is right
    assert p.gain_paise == 0 and not first_move_advice(p, {"x": X, "y": Y}, {})["hold"]


def _brute(cards, txns, states):
    best = float("-inf")
    def go(i, st, total):
        nonlocal best
        if i == len(txns):
            best = max(best, total); return
        opts = []
        for c in cards:
            ev = evaluate(c, txns[i], st.get(c.id), explain=False)
            if ev.eligible:
                opts.append((c, ev))
        if not opts:
            go(i + 1, st, total); return
        for c, ev in opts:
            ns = {k: CycleState(dict(v.reward_by_bucket_paise)) for k, v in st.items()}
            commit(c, txns[i], ns.setdefault(c.id, CycleState()), explain=False)
            go(i + 1, ns, total + ev.net_paise)
    go(0, {k: CycleState(dict(v.reward_by_bucket_paise)) for k, v in (states or {}).items()}, 0)
    return best


def test_matches_exhaustive_search_and_never_loses_to_greedy_on_random_cases():
    rnd = random.Random(7)
    cards = list(load_cards().values()) + [X, Y]
    mcc_merchants = [("5814", "swiggy"), ("5311", "amazon"), ("5411", "bigbasket"), ("5812", None), ("5541", None), ("7011", "smartbuy_hotels")]
    for _ in range(120):
        wallet = rnd.sample(cards, rnd.randint(2, 4))
        txns = []
        for _ in range(rnd.randint(1, 5)):
            mcc, m = rnd.choice(mcc_merchants)
            txns.append(Transaction(rnd.choice([30_000, 99_900, 250_000, 1_200_000, 4_000_000]), mcc, rnd.choice(list(Channel)), m))
        states = {c.id: CycleState({b: rnd.choice([0, 50_000, 140_000]) for b in ("swiggy", "online_5", "dx", "smartbuy", "upi", "cashback_total")})
                  for c in wallet if rnd.random() < 0.5}
        p = plan(wallet, txns, states)
        assert p.total_paise >= p.greedy_total_paise
        assert p.optimal and p.total_paise == _brute(wallet, txns, states)


def test_tiny_node_budget_degrades_gracefully_and_says_so():
    txns = [dinner(5000), dinner(10_000)] * 6
    p = plan([X, Y], txns, node_budget=3)
    assert not p.optimal and p.total_paise >= p.greedy_total_paise


def test_offers_are_part_of_the_objective():
    book = OfferBook.from_data({"offers": [{"id": "o", "merchants": ["amazon"], "cards": ["y"], "channels": ["online"], "discount_bps": 1000}]})
    t = Transaction(100_000, "5311", Channel.ONLINE, "amazon")
    p = plan([X, Y], [t], offers=book)
    assert p.steps[0].card_id == "y" and p.steps[0].value_paise == 4_000 + 10_000


def test_ineligible_everywhere_is_a_step_with_no_card():
    t = Transaction(100_000, "5311", Channel.UPI, None)           # neither test card supports UPI? Y/X support default channels only
    p = plan([X, Y], [t])
    assert p.steps[0].card_id is None and p.total_paise == 0


def test_plan_endpoint_shape_and_gain(make_client):
    c = make_client()
    txs = [{"amount_paise": 1_000_000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}] * 4
    r = c.post("/api/plan", json={"transactions": txs, "wallet": ["hdfc_swiggy", "sbi_cashback", "hdfc_infinia"], "include_offers": False}).json()
    assert r["gain_paise"] >= 0 and r["total_paise"] == r["greedy_total_paise"] + r["gain_paise"]
    assert len(r["steps"]) == 4 and {"card_id", "greedy_card_id", "differs", "value_paise"} <= set(r["steps"][0])
    assert r["first_move"]["card_id"] and isinstance(r["optimal"], bool)


def test_plan_endpoint_limits_and_unknown_cards(make_client):
    c = make_client(plan_max_txns=3)
    t = {"amount_paise": 100_000, "mcc": "5814", "channel": "online"}
    assert c.post("/api/plan", json={"transactions": [t] * 4, "wallet": ["hdfc_swiggy"]}).status_code == 422
    assert c.post("/api/plan", json={"transactions": [t], "wallet": ["nope"]}).status_code == 422


def test_plan_uses_stored_usage_for_a_user(make_client):
    c = make_client()
    uid = "planner-user-1"
    t = {"amount_paise": 1_000_000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}
    for i in range(2):                                              # burn most of the Swiggy cap server-side
        assert c.post("/api/commit", json={**body(amount=10_000, wallet=["hdfc_swiggy", "hdfc_infinia"]), "user_id": uid, "txn_ref": f"burn-cap-{i}"}).status_code == 200
    r = c.post("/api/plan", json={"transactions": [t], "wallet": ["hdfc_swiggy", "hdfc_infinia"], "user_id": uid, "include_offers": False}).json()
    assert r["steps"][0]["card_id"] == "hdfc_infinia"               # cap used up: the stored usage drove the plan


REAL_SCENARIO = [  # (paise, mcc, merchant, channel): festive shopping; every payment is possible on its channel
    (450_000, "5814", "swiggy", "upi"), (2_500_000, "5651", "myntra", "online"), (2_500_000, "5311", "amazon", "online"),
    (2_500_000, "5411", "bigbasket", "online"), (200_000, "5499", None, "online")]


def test_real_catalog_lookahead_gain():
    """Greedy sends the Myntra order to Swiggy HDFC and the Amazon order to Infinia. The plan pays Myntra with SBI and Amazon with
    Swiggy HDFC instead, because the cap each one burns would otherwise be missing where it pays more: +Rs 425 on about Rs 4,900."""
    cards = list(load_cards().values())
    txns = [Transaction(a, m, Channel(ch), mer) for a, m, mer, ch in REAL_SCENARIO]
    p = plan(cards, txns, {}, OfferBook.load())
    assert p.optimal and p.gain_paise == 42_500
    assert [s.card_id for s in p.greedy_steps][1:3] == ["hdfc_swiggy", "hdfc_infinia"]
    assert [s.card_id for s in p.steps][1:3] == ["sbi_cashback", "hdfc_swiggy"]


def test_real_catalog_lookahead_over_the_api(make_client):
    c = make_client()
    txs = [{"amount_paise": a, "mcc": m, "channel": ch, "merchant": mer} for a, m, mer, ch in REAL_SCENARIO]
    r = c.post("/api/plan", json={"transactions": txs, "wallet": ["amazon_icici", "hdfc_swiggy", "hdfc_infinia", "sbi_cashback", "tata_neu_rupay"]}).json()
    assert r["gain_paise"] == 42_500 and r["optimal"] and [s["differs"] for s in r["steps"]] == [False, True, True, False, False]
