import pytest

from rewards_engine import Channel, CycleState, Transaction, commit, evaluate, optimize
from rewards_engine.catalog import load_cards

CARDS = load_cards()
ALL = list(CARDS.values())


def T(rupees, mcc, channel=Channel.ONLINE, merchant=None):
    return Transaction(int(rupees * 100), mcc, channel, merchant)


def best(txn, wallet=ALL, states=None):
    return optimize(wallet, txn, states).best


def test_amazon_picks_amazon_card():
    b = best(T(10_000, "5311", merchant="amazon"))
    assert b.card_id == "amazon_icici"
    assert b.reward_paise == 50_000  # 5%


def test_swiggy_picks_swiggy_card():
    b = best(T(1_000, "5814", merchant="swiggy"))
    assert b.card_id == "hdfc_swiggy"
    assert b.reward_paise == 10_000  # 10%


def test_generic_offline_spend_picks_highest_base():
    b = best(T(5_000, "5411", Channel.TAP))
    assert b.card_id == "hdfc_infinia"  # 3.3% base


def test_fuel_excluded_and_surcharge_applied():
    # below the Rs 500 waiver floor: 1% surcharge, zero reward
    ev = evaluate(CARDS["sbi_cashback"], T(300, "5541", Channel.CARD))
    assert ev.reward_paise == 0
    assert ev.surcharge_paise == 300
    assert ev.net_paise == -300


def test_fuel_surcharge_waived_in_window():
    ev = evaluate(CARDS["sbi_cashback"], T(1_000, "5541", Channel.CARD))
    assert ev.surcharge_paise == 0 and "waived" in " ".join(ev.explanation)


def test_rent_has_negative_value_everywhere():
    rec = optimize(ALL, T(50_000, "6513", Channel.ONLINE))
    assert all(e.net_paise <= 0 for e in rec.ranked if e.eligible)


def test_upi_only_rupay():
    rec = optimize(ALL, T(500, "5411", Channel.UPI))
    assert rec.best.card_id == "tata_neu_rupay"
    ineligible = {e.card_id for e in rec.ranked if not e.eligible}
    assert ineligible == {"sbi_cashback", "amazon_icici", "hdfc_swiggy", "hdfc_infinia"}


def test_cap_overflow_falls_back_to_base():
    # swiggy cap: 1500 INR reward. At 10%, 20k spend would earn 2000 -> capped.
    c = CARDS["hdfc_swiggy"]
    ev = evaluate(c, T(20_000, "5814", merchant="swiggy"))
    # 15k at 10% = 1500, remaining 5k at 1% = 50
    assert ev.reward_paise == 150_000 + 5_000


def test_cap_exhaustion_changes_winner():
    wallet = [CARDS["hdfc_swiggy"], CARDS["hdfc_infinia"]]
    txn = T(2_000, "5814", merchant="swiggy")
    states = {"hdfc_swiggy": CycleState({"swiggy": 150_000})}  # cap used up
    assert best(txn, wallet).card_id == "hdfc_swiggy"
    assert best(txn, wallet, states).card_id == "hdfc_infinia"


def test_commit_consumes_cap_and_is_monotonic():
    c = CARDS["hdfc_swiggy"]
    s = CycleState()
    prev = None
    for _ in range(20):
        ev = commit(c, T(2_000, "5814", merchant="swiggy"), s)
        if prev is not None:
            assert ev.reward_paise <= prev  # reward never rises as cap drains
        prev = ev.reward_paise
    assert s.used("swiggy") == 150_000  # never exceeds cap


def test_reward_never_below_base_rate():
    c = CARDS["hdfc_swiggy"]
    s = CycleState({"swiggy": 150_000})
    ev = evaluate(c, T(1_000, "5814", merchant="swiggy"), s)
    assert ev.reward_paise == 1_000  # 1% base


def test_limit_check():
    from dataclasses import replace
    c = replace(CARDS["amazon_icici"], available_limit_paise=100)
    ev = evaluate(c, T(10_000, "5311", merchant="amazon"))
    assert not ev.eligible


def test_invalid_transactions():
    with pytest.raises(ValueError):
        Transaction(0, "5411", Channel.TAP)
    with pytest.raises(ValueError):
        Transaction(100, "54", Channel.TAP)


def test_deterministic_ranking():
    a = optimize(ALL, T(1_234.56, "5812", Channel.CARD))
    b = optimize(list(reversed(ALL)), T(1_234.56, "5812", Channel.CARD))
    assert [e.card_id for e in a.ranked] == [e.card_id for e in b.ranked]


def test_no_eligible_card():
    rec = optimize([CARDS["amazon_icici"]], T(100, "5411", Channel.UPI))
    assert rec.best is None


def test_tie_prefers_uncapped_rule_to_preserve_caps():
    # Amazon 5% (uncapped) ties with SBI/Swiggy 5% online (capped): keep the caps for later
    b = best(T(2_500, "5311", merchant="amazon"))
    assert b.card_id == "amazon_icici" and not b.uses_cap
    wallet = [CARDS["sbi_cashback"], CARDS["tata_neu_rupay"]]
    assert best(T(2_500, "5311", merchant="amazon"), wallet).card_id == "sbi_cashback"
    # tie between capped SBI and an uncapped 5% card named later alphabetically
    wallet = [CARDS["sbi_cashback"], CARDS["tata_neu_rupay"]]
    assert best(T(2_500, "5411", merchant="bigbasket"), wallet).card_id == "tata_neu_rupay"
