"""Differential test: optimized engine vs the frozen original (tests/reference_engine.py).

Randomized (fixed seed, `random` since hypothesis is not a dependency) transactions x wallets x
cap states, on both the real catalog and synthetic cards built to stress rule matching (ties,
duplicate rule ids, multi-constraint rules, min_txn thresholds, shared buckets, zero rates).
"""
import copy
import random
from dataclasses import fields

import pytest

from scruge import engine as cur
from scruge.catalog import load_cards
from scruge.mcc import MCC_CATEGORIES
from scruge.models import Card, Channel, CycleState, EarnRule, Network, Transaction
from tests import reference_engine as ref

SEED = 20240607
CARDS = list(load_cards().values())
MCCS = list(MCC_CATEGORIES) + ["0001", "9999", "5411"]
MERCHANTS = [None, "", "amazon", "swiggy", "bigbasket", "croma", "uber", "smartbuy_hotels", "zzz", "m1", "m2", "m3"]
CATS = sorted(set(MCC_CATEGORIES.values())) + ["other"]
CHANNELS = list(Channel)


def same(a, b, explain=True):
    for f in fields(b):
        if f.name == "explanation":
            if explain:
                assert list(a.explanation) == list(b.explanation)
            else:
                assert len(a.explanation) == 0
        else:
            assert getattr(a, f.name) == getattr(b, f.name), f.name


def rand_txn(rnd):
    amt = rnd.choice([1, 99, 100, 39_999, 40_000, 50_000, 500_000, 500_001, 1_000_000, 123_457,
                      rnd.randint(1, 10_000_000), rnd.randint(1, 100_000_000_00)])
    return Transaction(amt, rnd.choice(MCCS), rnd.choice(CHANNELS), rnd.choice(MERCHANTS))


def rand_states(rnd, cards):
    states = {}
    for c in cards:
        if rnd.random() < 0.6:
            buckets = {(r.cap_bucket or r.id) for r in c.rules} | {"unrelated"}
            st = {}
            for b in buckets:
                if rnd.random() < 0.7:
                    caps = [r.cap_paise for r in c.rules if (r.cap_bucket or r.id) == b and r.cap_paise]
                    top = (max(caps) if caps else 1000) * 1.3
                    st[b] = rnd.choice([0, 1, int(top), rnd.randint(0, int(top))])
            states[c.id] = CycleState(st)
    return states


def rand_card(rnd, i):
    rules = []
    for j in range(rnd.randint(0, 7)):
        rules.append(EarnRule(
            id=rnd.choice([f"r{j}", f"r{j}", "dup"]),
            rate_bps=rnd.choice([0, 50, 100, 100, 500, 500, 1000, 3300]),
            mccs=frozenset(rnd.sample(MCCS, rnd.randint(0, 2))) if rnd.random() < .3 else frozenset(),
            categories=frozenset(rnd.sample(CATS, rnd.randint(1, 3))) if rnd.random() < .35 else frozenset(),
            merchants=frozenset(rnd.sample(MERCHANTS, rnd.randint(1, 3))) if rnd.random() < .35 else frozenset(),
            channels=frozenset(rnd.sample(CHANNELS, rnd.randint(1, 3))) if rnd.random() < .4 else frozenset(),
            cap_paise=rnd.choice([None, None, 1000, 50_000, 150_000]),
            cap_bucket=rnd.choice([None, "b1", "b2"]),
            min_txn_paise=rnd.choice([0, 0, 40_000, 500_000]),
        ))
    return Card(
        id=f"c{i}", name=f"Card {i}", network=rnd.choice(list(Network)),
        base_rate_bps=rnd.choice([0, 100, 250]), rules=tuple(rules),
        excluded_categories=frozenset(rnd.sample(CATS, rnd.randint(0, 2))),
        excluded_mccs=frozenset(rnd.sample(MCCS, rnd.randint(0, 1))),
        surcharge_bps={c: rnd.choice([50, 100]) for c in rnd.sample(CATS, rnd.randint(0, 2))},
        surcharge_waiver={c: (rnd.choice([1, 40_000]), rnd.choice([500_000, 10])) for c in rnd.sample(CATS, rnd.randint(0, 1))},
        supported_channels=frozenset(rnd.sample(CHANNELS, rnd.randint(1, 4))),
        available_limit_paise=rnd.choice([None, None, 1_000_000, 100]),
    )


def check_case(wallet, txn, states):
    for explain in (True, False):
        a = cur.optimize(wallet, txn, copy.deepcopy(states), explain=explain)
        b = ref.optimize(wallet, txn, copy.deepcopy(states))
        assert [e.card_id for e in a.ranked] == [e.card_id for e in b.ranked]
        for x, y in zip(a.ranked, b.ranked):
            same(x, y, explain)
        assert (a.best is None) == (b.best is None)
        if a.best:
            assert a.best.card_id == b.best.card_id
    for c in wallet:
        same(cur.evaluate(c, txn, states.get(c.id)), ref.evaluate(c, txn, states.get(c.id)))


def test_real_catalog_randomized():
    rnd = random.Random(SEED)
    for _ in range(4000):
        wallet = [rnd.choice(CARDS) for _ in range(rnd.randint(1, 7))] if rnd.random() < .3 \
            else rnd.sample(CARDS, rnd.randint(1, len(CARDS)))
        check_case(wallet, rand_txn(rnd), rand_states(rnd, wallet))


def test_synthetic_cards_randomized():
    rnd = random.Random(SEED + 1)
    for n in range(300):
        cards = [rand_card(rnd, i) for i in range(rnd.randint(1, 5))]
        for _ in range(40):
            check_case(cards, rand_txn(rnd), rand_states(rnd, cards))


@pytest.mark.parametrize("synthetic", [False, True])
def test_commit_sequences_match(synthetic):
    """Month simulation: sequential commits leave identical cap state and evaluations."""
    rnd = random.Random(SEED + 2)
    for _ in range(150):
        cards = [rand_card(rnd, i) for i in range(3)] if synthetic else rnd.sample(CARDS, 3)
        s_cur, s_ref = {}, {}
        for _ in range(40):
            txn = rand_txn(rnd)
            card = rnd.choice(cards)
            a = cur.commit(card, txn, s_cur.setdefault(card.id, CycleState()))
            b = ref.commit(card, txn, s_ref.setdefault(card.id, CycleState()))
            same(a, b)
            assert {k: v.reward_by_bucket_paise for k, v in s_cur.items()} == \
                   {k: v.reward_by_bucket_paise for k, v in s_ref.items()}


def test_consume_after_optimize_equals_commit():
    """The batch endpoint reuses optimize()'s evaluation for cap consumption; must equal commit()."""
    rnd = random.Random(SEED + 3)
    for _ in range(300):
        cards = rnd.sample(CARDS, 4)
        txn = rand_txn(rnd)
        st1, st2 = rand_states(rnd, cards), None
        st2 = copy.deepcopy(st1)
        rec = cur.optimize(cards, txn, st1, explain=False)
        if rec.best is None:
            continue
        card = next(c for c in cards if c.id == rec.best.card_id)
        cur.consume(card, txn, st1.setdefault(card.id, CycleState()), rec.best)
        ref.commit(card, txn, st2.setdefault(card.id, CycleState()))
        assert {k: v.reward_by_bucket_paise for k, v in st1.items()} == \
               {k: v.reward_by_bucket_paise for k, v in st2.items()}
