"""Reward engine. For each card in the wallet:

1. Eligibility: channel supported, MCC/category not excluded, limit available.
2. Rule match: the most specific rule wins (merchant > MCC > category > channel), else the base rate.
3. Caps: reward is clipped to what is left in the rule's cap bucket this cycle; spend above the cap earns the base rate.
4. Net value = reward - surcharge.

Cards rank by net value; ties go to the one that uses less cap, which keeps scarce caps for later.
tests/test_differential.py pins this module to a frozen copy of the original on random inputs.
"""
from __future__ import annotations

from dataclasses import dataclass

from .mcc import category_for
from .models import Card, CycleState, EarnRule, Evaluation, Transaction

_NO_WHY: tuple[str, ...] = ()


def _bps(amount_paise: int, bps: int) -> int:
    # floor: issuers round down reward fractions
    return amount_paise * bps // 10_000


def _select_rule(card: Card, txn: Transaction, category: str) -> EarnRule | None:
    """Most specific matching rule (ties: higher rate, then earliest declared)."""
    idx = card.index
    if not idx.has_rules:
        return None
    amount = txn.amount_paise
    channel = txn.channel
    mcc = txn.mcc
    merchant = txn.merchant or ""
    best_key = None
    best = None
    for bucket in (idx.by_merchant.get(merchant), idx.by_mcc.get(mcc),
                   idx.by_category.get(category), idx.general):
        if not bucket:
            continue
        for key, r in bucket:
            if amount < r.min_txn_paise:
                continue
            if r.channels and channel not in r.channels:
                continue
            if r.merchants and merchant not in r.merchants:
                continue
            if r.mccs and mcc not in r.mccs:
                continue
            if r.categories and category not in r.categories:
                continue
            if best_key is None or key > best_key:
                best_key = key
                best = r
    return best


def evaluate(card: Card, txn: Transaction, state: CycleState | None = None,
             explain: bool = True) -> Evaluation:
    return _evaluate(card, txn, state, explain, category_for(txn.mcc))


def _evaluate(card: Card, txn: Transaction, state: CycleState | None, explain: bool,
              category: str) -> Evaluation:
    amount = txn.amount_paise
    why: list[str] | tuple[str, ...] = [f"MCC {txn.mcc} → category '{category}'"] if explain else _NO_WHY

    if txn.channel not in card.supported_channels:
        if explain:
            why.append(f"{card.network.value} card can't pay via {txn.channel.value}")
        return Evaluation(card.id, card.name, False, 0, 0, 0, None, 0, why)
    if card.available_limit_paise is not None and amount > card.available_limit_paise:
        if explain:
            why.append("insufficient available limit")
        return Evaluation(card.id, card.name, False, 0, 0, 0, None, 0, why)

    surcharge = 0
    if category in card.surcharge_bps:
        lo, hi = card.surcharge_waiver.get(category, (1, 0))
        if lo <= amount <= hi:
            if explain:
                why.append(f"{category} surcharge waived")
        else:
            surcharge = _bps(amount, card.surcharge_bps[category])
            if explain:
                why.append(f"{category} surcharge ₹{surcharge / 100:.2f}")

    if category in card.excluded_categories or txn.mcc in card.excluded_mccs:
        if explain:
            why.append(f"'{category}' excluded from rewards")
        return Evaluation(card.id, card.name, True, 0, surcharge, -surcharge, None, 0, why)

    rule = _select_rule(card, txn, category)

    base_reward = _bps(amount, card.base_rate_bps)
    if rule is None:
        reward = base_reward
        if explain:
            why.append(f"base rate {card.base_rate_bps / 100:.2f}% → ₹{reward / 100:.2f}")
    else:
        full = _bps(amount, rule.rate_bps)
        reward = full
        if rule.cap_paise is not None:
            bucket = rule.cap_bucket or rule.id
            used = state.reward_by_bucket_paise.get(bucket, 0) if state is not None else 0
            remaining = max(0, rule.cap_paise - used)
            if full > remaining:
                # portion of spend that fits under the cap earns the accelerated
                # rate; the rest falls back to base rate
                covered_spend = remaining * 10_000 // rule.rate_bps if rule.rate_bps else 0
                overflow = amount - covered_spend
                reward = remaining + _bps(overflow, card.base_rate_bps)
                if explain:
                    why.append(
                        f"rule '{rule.id}' {rule.rate_bps / 100:.2f}% capped: "
                        f"₹{remaining / 100:.2f} left in '{bucket}', rest at base"
                    )
            elif explain:
                why.append(f"rule '{rule.id}' {rule.rate_bps / 100:.2f}% → ₹{full / 100:.2f}")
        elif explain:
            why.append(f"rule '{rule.id}' {rule.rate_bps / 100:.2f}% → ₹{full / 100:.2f}")
        if reward < base_reward:  # never worse than base
            reward = base_reward

    net = reward - surcharge
    return Evaluation(card.id, card.name, True, reward, surcharge, net,
                      rule.id if rule else None, net * 10_000 // amount, why,
                      rule is not None and rule.cap_paise is not None)


@dataclass(frozen=True)
class Recommendation:
    best: Evaluation | None
    ranked: list[Evaluation]


def _rank_key(e: Evaluation):
    return (-e.net_paise, e.uses_cap, e.card_id)


def optimize(
    wallet: list[Card],
    txn: Transaction,
    states: dict[str, CycleState] | None = None,
    explain: bool = True,
) -> Recommendation:
    states = states or {}
    category = category_for(txn.mcc)
    evals = [_evaluate(c, txn, states.get(c.id), explain, category) for c in wallet]
    eligible = [e for e in evals if e.eligible]
    eligible.sort(key=_rank_key)
    ranked = eligible + [e for e in evals if not e.eligible]
    return Recommendation(best=eligible[0] if eligible else None, ranked=ranked)


def consume(card: Card, txn: Transaction, state: CycleState, ev: Evaluation) -> None:
    """Apply the cap consumption implied by `ev` (an evaluation of txn against `state`)."""
    if ev.rule_id:
        rule = card.index.rule_by_id[ev.rule_id]
        if rule.cap_paise is not None:
            bucket = rule.cap_bucket or rule.id
            remaining = max(0, rule.cap_paise - state.used(bucket))
            state.add(bucket, min(remaining, _bps(txn.amount_paise, rule.rate_bps)))


def commit(card: Card, txn: Transaction, state: CycleState, explain: bool = True) -> Evaluation:
    """Record a transaction against a card's cycle caps. Returns its evaluation."""
    ev = evaluate(card, txn, state, explain)
    consume(card, txn, state, ev)
    return ev
