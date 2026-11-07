"""Choose cards for a sequence of payments together, not one at a time.

Greedy spends a scarce cap on whichever payment comes first. Example (tests/test_planner.py): card X pays 10% on dining
up to Rs 1,000 a cycle, card Y pays 4% uncapped. For a Rs 5,000 dinner then a Rs 10,000 dinner, greedy picks X twice
(Rs 1,050); planning picks Y then X (Rs 1,200).

The search is depth-first branch and bound over (payment -> card), seeded with the greedy plan so it is never worse.
A branch is cut when its value so far plus the best possible for the rest cannot beat the best plan found; "best
possible" is each payment's value with every cap untouched. If the node budget runs out, the best plan so far is
returned with optimal=False. The objective is reward net of surcharge plus any offer. All money is integer paise.
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import consume, evaluate
from .models import Card, CycleState, Evaluation, Transaction
from .offers import EMPTY, OfferBook, with_offer

DEFAULT_NODE_BUDGET = 150_000


@dataclass(frozen=True)
class Step:
    index: int
    card_id: str | None        # None: no eligible card for this payment
    value_paise: int           # net reward + offer for this payment, given the caps at that moment
    uses_cap: bool = False


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]
    total_paise: int
    greedy_steps: tuple[Step, ...]
    greedy_total_paise: int
    nodes: int
    optimal: bool              # False if the node budget ran out before the search finished

    @property
    def gain_paise(self) -> int:
        return self.total_paise - self.greedy_total_paise


def _copy_states(states: dict[str, CycleState] | None) -> dict[str, CycleState]:
    return {cid: CycleState(dict(s.reward_by_bucket_paise)) for cid, s in (states or {}).items()}


def _candidates(cards: list[Card], txn: Transaction, states: dict[str, CycleState], offers: OfferBook, blocked=None,
                ousage: dict[str, int] | None = None):
    """Eligible (value, uses_cap, card, evaluation) sorted best-first, same tie-break as the engine."""
    if blocked is not None and blocked(txn):       # e.g. a contactless payment above the PIN limit: no card can do it this way
        return []
    out = []
    for c in cards:
        ev = evaluate(c, txn, states.get(c.id), explain=False)
        if ev.eligible:
            a = offers.best_for(c.id, txn, ousage)
            ev, a = with_offer(ev, a)
            out.append((ev.net_paise + (a.paise if a else 0), ev.uses_cap, c, ev, a))
    out.sort(key=lambda t: (-t[0], t[1], t[2].id))
    return out


def _apply(card: Card, txn: Transaction, states: dict[str, CycleState], ev: Evaluation) -> dict[str, CycleState]:
    """New state mapping with only `card`'s CycleState copied (copy-on-write keeps the search cheap)."""
    if not ev.uses_cap:
        return states
    new = dict(states)
    st = CycleState(dict(states[card.id].reward_by_bucket_paise)) if card.id in states else CycleState()
    consume(card, txn, st, ev)
    new[card.id] = st
    return new


def plan(cards: list[Card], txns: list[Transaction], states: dict[str, CycleState] | None = None,
         offers: OfferBook = EMPTY, node_budget: int = DEFAULT_NODE_BUDGET, blocked=None,
         offer_usage: dict[str, int] | None = None) -> Plan:
    start = _copy_states(states)
    ostart = dict(offer_usage or {})
    n = len(txns)

    def spend(ousage: dict[str, int], a) -> dict[str, int]:
        """Offer usage after taking offer `a` (copy-on-write, like the cap states)."""
        if a is None or a.offer.cycle_cap_paise is None:
            return ousage
        return {**ousage, a.offer.id: ousage.get(a.offer.id, 0) + a.paise}

    # greedy incumbent (what one-at-a-time optimisation would do)
    g_steps: list[Step] = []
    g_total, st, ou = 0, start, ostart
    for i, t in enumerate(txns):
        cand = _candidates(cards, t, st, offers, blocked, ou)
        if not cand:
            g_steps.append(Step(i, None, 0)); continue
        v, uses_cap, c, ev, a = cand[0]
        g_steps.append(Step(i, c.id, v, uses_cap)); g_total += v
        st, ou = _apply(c, t, st, ev), spend(ou, a)

    # upper bound per payment: best value with every cap and offer limit untouched (they only ever reduce value)
    fresh: dict[str, CycleState] = {}
    ub = []
    for t in txns:
        cand = _candidates(cards, t, fresh, offers, blocked)
        ub.append(cand[0][0] if cand else 0)
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + ub[i]

    best_total, best_steps = g_total, list(g_steps)
    nodes = 0
    exhausted = False
    path: list[Step] = []

    def dfs(i: int, total: int, states_i: dict[str, CycleState], ou_i: dict[str, int]) -> None:
        nonlocal best_total, best_steps, nodes, exhausted
        if exhausted:
            return
        if i == n:
            if total > best_total:          # strictly better only: ties keep the greedy plan
                best_total, best_steps = total, list(path)
            return
        if total + suffix[i] <= best_total:
            return
        cand = _candidates(cards, txns[i], states_i, offers, blocked, ou_i)
        if not cand:
            path.append(Step(i, None, 0)); dfs(i + 1, total, states_i, ou_i); path.pop()
            return
        for v, uses_cap, c, ev, a in cand:
            nodes += 1
            if nodes > node_budget:
                exhausted = True
                return
            if total + v + suffix[i + 1] <= best_total:
                continue
            path.append(Step(i, c.id, v, uses_cap))
            dfs(i + 1, total + v, _apply(c, txns[i], states_i, ev), spend(ou_i, a))
            path.pop()

    dfs(0, 0, start, ostart)
    return Plan(tuple(best_steps), best_total, tuple(g_steps), g_total, nodes, not exhausted)


def first_move_advice(p: Plan, cards: dict[str, Card], states: dict[str, CycleState] | None = None) -> dict:
    """What to do with the *current* payment (step 0), and why, in plain language."""
    if not p.steps:
        return {"card_id": None, "hold": False, "reason": "No payments to plan."}
    s, g = p.steps[0], p.greedy_steps[0]
    if s.card_id == g.card_id or g.card_id is None:
        return {"card_id": s.card_id, "greedy_card_id": g.card_id, "hold": False,
                "reason": "The best card now is also the best for the month ahead."}
    gc = cards[g.card_id]
    held = sorted({(r.cap_bucket or r.id, r.cap_paise) for r in gc.rules if r.cap_paise})
    left = ""
    if held and states is not None:
        b, cap = held[0]
        left = f" (₹{max(0, cap - states.get(gc.id, CycleState()).used(b)) / 100:,.0f} of rewards left)"
    return {"card_id": s.card_id, "greedy_card_id": g.card_id, "hold": True,
            "gain_paise": p.gain_paise,
            "reason": f"Hold {gc.name}'s capped rewards{left}: bigger payments later use them better. "
                      f"Paying with {cards[s.card_id].name} now earns ₹{p.gain_paise / 100:,.2f} more over these payments."}
