"""Did the issuer credit what we predicted? Compares the reward expected at payment time with what the statement says
was credited, and works out why when they differ.

Statuses: matched (within max(Rs 1, 2%)), short, over (our rules are probably stale), pending (inside the settlement
window), overdue (no credit after the window, treated as short by the full amount).

For a mismatch it tests what the issuer may have done using the card's own rules: paid the base rate, paid another
rule's rate, paid zero (excluded), or capped the reward lower. The first that reproduces the credited amount wins;
otherwise the cause is "unexplained". A short credit also gets a dispute note. All money is integer paise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Card

ABS_TOL_PAISE = 100          # Rs 1
REL_TOL_BPS = 200            # 2%
SETTLEMENT_DAYS = 45         # most issuers credit by the next statement


def tolerance(expected_paise: int) -> int:
    return max(ABS_TOL_PAISE, expected_paise * REL_TOL_BPS // 10_000)


@dataclass(frozen=True)
class Expected:
    txn_ref: str
    card_id: str
    amount_paise: int
    expected_paise: int
    created_at: str                    # ISO 8601
    rule_id: str | None = None
    merchant: str | None = None
    mcc: str | None = None
    rules_version: str | None = None


@dataclass(frozen=True)
class Credit:
    txn_ref: str
    actual_paise: int
    credited_at: str | None = None


def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def diagnose(card: Card | None, e: Expected, actual_paise: int) -> dict:
    """Best explanation for `actual_paise` given what we expected, from the card's own rules."""
    if card is None:
        return {"cause": "unexplained", "detail": "Card rules unavailable for this payment."}
    amt = e.amount_paise
    if actual_paise == 0:
        return {"cause": "no_reward", "detail": "Issuer credited nothing: the category may be excluded or the payment not eligible."}
    hyps: list[tuple[str, str, int]] = [
        ("base_rate", f"Issuer paid the card's base rate ({card.base_rate_bps / 100:.2f}%): the accelerated rule did not apply "
                      "(merchant not recognised, wrong channel, or minimum not met).", amt * card.base_rate_bps // 10_000)]
    for r in card.rules:
        hyps.append(("other_rule", f"Issuer paid rule '{r.id}' ({r.rate_bps / 100:.2f}%).", amt * r.rate_bps // 10_000))
    over = actual_paise > e.expected_paise
    for h in hyps:
        if abs(h[2] - actual_paise) <= 1:
            if over:        # the issuer paid MORE than we predicted: our rules are stale, there is nothing to dispute
                return {"cause": h[0], "detail": h[1].rstrip(".") + ". It is higher than we predicted, so our rules for this card are probably out of date: check them, there is nothing to dispute."}
            return {"cause": h[0], "detail": h[1]}
    if over:
        return {"cause": "over_credit", "detail": "The issuer credited more than we predicted and no known rate on this card explains it. Our rules are probably out of date: check them. There is nothing to dispute."}
    # A credit between the base rate and the expected amount fits the applied rule's cap running out earlier in the cycle.
    # Only that rule can explain it; a rule with no cap cannot be "capped".
    rule = next((r for r in card.rules if r.id == e.rule_id), None) if e.rule_id else None
    if rule is not None and rule.cap_paise is not None and actual_paise < e.expected_paise:
        base = amt * card.base_rate_bps // 10_000
        if base <= actual_paise:
            bucket = rule.cap_bucket or rule.id
            return {"cause": "cap_reached", "rule": rule.id,
                    "detail": f"Consistent with the issuer's cap on '{rule.id}' (₹{rule.cap_paise / 100:,.0f} a cycle, shared bucket '{bucket}') "
                              "being used up earlier in the cycle; we assumed more cap was left."}
    return {"cause": "unexplained", "detail": "Credit does not match any rate on this card. Worth disputing."}


def dispute_note(e: Expected, actual_paise: int, diagnosis: dict, card_name: str) -> str:
    return (f"Reward query for transaction {e.txn_ref} on {e.created_at[:10]} "
            f"({e.merchant or 'merchant'}, ₹{e.amount_paise / 100:,.2f}) on {card_name}. "
            f"Expected reward ₹{e.expected_paise / 100:,.2f}"
            f"{' under rule ' + e.rule_id if e.rule_id else ''}; credited ₹{actual_paise / 100:,.2f}. "
            f"Likely cause: {diagnosis['detail']} Please review and credit the difference of "
            f"₹{(e.expected_paise - actual_paise) / 100:,.2f}.")


def reconcile(expected: list[Expected], credits: list[Credit], cards: dict[str, Card] | None = None,
              now: datetime | None = None, settlement_days: int = SETTLEMENT_DAYS, card_on=None) -> dict:
    now = now or datetime.now(timezone.utc)
    by_ref = {c.txn_ref: c for c in credits}
    items, tot_expected, tot_credited, tot_short = [], 0, 0, 0
    counts = {"matched": 0, "short": 0, "over": 0, "pending": 0, "overdue": 0}
    for e in expected:
        c = by_ref.get(e.txn_ref)
        card = (cards or {}).get(e.card_id)
        if card is not None and card_on is not None:
            card = card_on(card, (e.created_at or "")[:10] or None)      # diagnose against the rules in force when it was paid
        item = {"txn_ref": e.txn_ref, "card_id": e.card_id, "expected_paise": e.expected_paise,
                "actual_paise": None, "diff_paise": None}
        if c is None:
            due = _parse(e.created_at) + timedelta(days=settlement_days)
            status = "overdue" if now > due else "pending"
            item.update(status=status, due_by=due.date().isoformat())
            if status == "overdue":
                tot_short += e.expected_paise
                d = {"cause": "not_credited", "detail": f"No credit found {settlement_days} days after the payment."}
                item.update(diagnosis=d, dispute_note=dispute_note(e, 0, d, card.name if card else e.card_id))
        else:
            diff = c.actual_paise - e.expected_paise
            tol = tolerance(e.expected_paise)
            status = "matched" if abs(diff) <= tol else ("short" if diff < 0 else "over")
            item.update(status=status, actual_paise=c.actual_paise, diff_paise=diff, tolerance_paise=tol)
            tot_credited += c.actual_paise
            if status != "matched":
                d = diagnose(card, e, c.actual_paise)
                item["diagnosis"] = d
                if status == "short":
                    tot_short += -diff
                    item["dispute_note"] = dispute_note(e, c.actual_paise, d, card.name if card else e.card_id)
        tot_expected += e.expected_paise
        counts[item["status"]] += 1
        items.append(item)
    return {"counts": counts, "expected_paise": tot_expected, "credited_paise": tot_credited,
            "shortfall_paise": tot_short, "items": items}
