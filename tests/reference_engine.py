# FROZEN COPY of the original scruge/engine.py (pre-optimisation). Do not edit: it is the oracle for
# tests/test_differential.py and the 'before' side of benchmarks/micro.py.
"""Rewards optimizer engine.

For each card in the wallet:
  1. Eligibility: channel supported, MCC/category not excluded, limit available.
  2. Rule match: pick the most specific matching EarnRule (merchant > MCC >
     category > channel), falling back to the card's base rate. Ties broken by
     higher rate (issuers apply the best applicable rule).
  3. Cap-aware reward: reward is clipped to remaining cap in the rule's bucket
     for this statement cycle. Spend above the cap earns the base rate
     (this is how most Indian issuers behave, e.g. SBI Cashback, HDFC SmartBuy).
  4. Net value = reward - surcharge (e.g. 1% fuel surcharge, 1% rent fee).

Cards are ranked by net value; ties broken by lower cap consumption, so we
preserve scarce accelerated caps for later transactions.
"""
from __future__ import annotations

from dataclasses import dataclass

from scruge.mcc import category_for
from scruge.models import Card, CycleState, EarnRule, Transaction


@dataclass(frozen=True)
class Evaluation:  # the original frozen result type (the live one is now a non-frozen slots class)
    card_id: str
    card_name: str
    eligible: bool
    reward_paise: int
    surcharge_paise: int
    net_paise: int
    rule_id: str | None
    effective_rate_bps: int
    explanation: list[str]
    uses_cap: bool = False


def _bps(amount_paise: int, bps: int) -> int:
    # floor: issuers round down reward fractions
    return amount_paise * bps // 10_000


def _matches(rule: EarnRule, txn: Transaction, category: str) -> bool:
    if txn.amount_paise < rule.min_txn_paise:
        return False
    if rule.channels and txn.channel not in rule.channels:
        return False
    if rule.merchants and (txn.merchant or "") not in rule.merchants:
        return False
    if rule.mccs and txn.mcc not in rule.mccs:
        return False
    if rule.categories and category not in rule.categories:
        return False
    return True


def evaluate(card: Card, txn: Transaction, state: CycleState | None = None) -> Evaluation:
    state = state or CycleState()
    category = category_for(txn.mcc)
    why: list[str] = [f"MCC {txn.mcc} → category '{category}'"]

    def ineligible(reason: str) -> Evaluation:
        why.append(reason)
        return Evaluation(card.id, card.name, False, 0, 0, 0, None, 0, why)

    if txn.channel not in card.supported_channels:
        return ineligible(f"{card.network.value} card can't pay via {txn.channel.value}")
    if card.available_limit_paise is not None and txn.amount_paise > card.available_limit_paise:
        return ineligible("insufficient available limit")

    surcharge = 0
    if category in card.surcharge_bps:
        lo, hi = card.surcharge_waiver.get(category, (1, 0))
        if lo <= txn.amount_paise <= hi:
            why.append(f"{category} surcharge waived")
        else:
            surcharge = _bps(txn.amount_paise, card.surcharge_bps[category])
            why.append(f"{category} surcharge ₹{surcharge / 100:.2f}")

    excluded = category in card.excluded_categories or txn.mcc in card.excluded_mccs
    if excluded:
        why.append(f"'{category}' excluded from rewards")
        return Evaluation(card.id, card.name, True, 0, surcharge, -surcharge, None, 0, why)

    candidates = [r for r in card.rules if _matches(r, txn, category)]
    rule = max(candidates, key=lambda r: (r.specificity, r.rate_bps), default=None)

    base_reward = _bps(txn.amount_paise, card.base_rate_bps)
    if rule is None:
        reward = base_reward
        why.append(f"base rate {card.base_rate_bps / 100:.2f}% → ₹{reward / 100:.2f}")
    else:
        full = _bps(txn.amount_paise, rule.rate_bps)
        reward = full
        if rule.cap_paise is not None:
            bucket = rule.cap_bucket or rule.id
            remaining = max(0, rule.cap_paise - state.used(bucket))
            if full > remaining:
                # portion of spend that fits under the cap earns the accelerated
                # rate; the rest falls back to base rate
                covered_spend = remaining * 10_000 // rule.rate_bps if rule.rate_bps else 0
                overflow = txn.amount_paise - covered_spend
                reward = remaining + _bps(overflow, card.base_rate_bps)
                why.append(
                    f"rule '{rule.id}' {rule.rate_bps / 100:.2f}% capped: "
                    f"₹{remaining / 100:.2f} left in '{bucket}', rest at base"
                )
            else:
                why.append(f"rule '{rule.id}' {rule.rate_bps / 100:.2f}% → ₹{full / 100:.2f}")
        else:
            why.append(f"rule '{rule.id}' {rule.rate_bps / 100:.2f}% → ₹{full / 100:.2f}")
        if reward < base_reward:  # never worse than base
            reward = base_reward

    net = reward - surcharge
    eff = net * 10_000 // txn.amount_paise
    return Evaluation(card.id, card.name, True, reward, surcharge, net,
                      rule.id if rule else None, eff, why,
                      uses_cap=bool(rule and rule.cap_paise is not None))


@dataclass(frozen=True)
class Recommendation:
    best: Evaluation | None
    ranked: list[Evaluation]


def optimize(
    wallet: list[Card],
    txn: Transaction,
    states: dict[str, CycleState] | None = None,
) -> Recommendation:
    states = states or {}
    evals = [evaluate(c, txn, states.get(c.id)) for c in wallet]
    eligible = [e for e in evals if e.eligible]
    eligible.sort(key=lambda e: (-e.net_paise, e.uses_cap, e.card_id))
    ranked = eligible + [e for e in evals if not e.eligible]
    return Recommendation(best=eligible[0] if eligible else None, ranked=ranked)


def commit(card: Card, txn: Transaction, state: CycleState) -> Evaluation:
    """Record a transaction against a card's cycle caps. Returns its evaluation."""
    ev = evaluate(card, txn, state)
    if ev.rule_id:
        rule = next(r for r in card.rules if r.id == ev.rule_id)
        if rule.cap_paise is not None:
            bucket = rule.cap_bucket or rule.id
            remaining = max(0, rule.cap_paise - state.used(bucket))
            state.add(bucket, min(remaining, _bps(txn.amount_paise, rule.rate_bps)))
    return ev
