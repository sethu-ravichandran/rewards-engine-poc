"""HTTP input to engine input, and engine output to the wire shape: money parsing, wallet and usage validation,
offers and channel rules applied over the engine's ranking, and one card's result.

Money on the wire is integer paise (amount_paise, cycle_usage_paise, *_paise). The older rupee float fields (amount,
cycle_usage, *_inr) are display-only and derived from paise. The response shape follows the request: amount_paise gets the
integer-only response; the legacy amount also gets the deprecated float fields.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from fastapi.responses import Response

from .catalog import MERCHANTS, CatalogSnapshot, card_on
from .channels import block_reason
from .config import Settings
from .models import CycleState, Evaluation, Transaction
from .offers import OfferBook, with_offer
from .schemas import TxnIn
from .userstore import CYCLE, USER_ID, current_cycle


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def json_response(obj, status: int = 200, headers: dict | None = None) -> Response:
    return Response(dumps(obj), status_code=status, media_type="application/json", headers=headers)


def valid_uid(uid: str) -> str:
    if not USER_ID.match(uid):
        raise HTTPException(422, "user id must be 8-64 characters of letters, digits, _ or -")
    return uid


def valid_cycle(cycle: str | None) -> str:
    if cycle is None:
        return current_cycle()
    if not CYCLE.match(cycle):
        raise HTTPException(422, "cycle must be a real month like 2026-11")
    return cycle


def _paise_from_rupees(x: float) -> int:
    """Deprecated rupee float -> paise through its decimal text, never by multiplying a binary float."""
    return int((Decimal(str(x)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def out(e: Evaluation, offer: dict | None = None, lean: bool = False, amount_paise: int = 0) -> dict:
    """One card's result. `*_paise` are authoritative. Lean (paise requests) carries integers only;
    the legacy shape adds the deprecated `*_inr` / `effective_rate_pct` floats for old clients."""
    off = offer["paise"] if offer else 0
    # rates, in basis points of the payment: `effective_rate_bps` is the card's reward net of surcharge (what the
    # issuer credits later); `offer_rate_bps` the instant discount; `total_rate_bps` is what the payment is really worth
    offer_bps = off * 10_000 // amount_paise if amount_paise else 0
    total_bps = (e.net_paise + off) * 10_000 // amount_paise if amount_paise else e.effective_rate_bps
    o = {"card_id": e.card_id, "card": e.card_name, "eligible": e.eligible,
         "reward_paise": e.reward_paise, "surcharge_paise": e.surcharge_paise, "net_paise": e.net_paise,
         "effective_rate_bps": e.effective_rate_bps, "offer_rate_bps": offer_bps, "total_rate_bps": total_bps,
         "rule": e.rule_id, "uses_cap": e.uses_cap, "why": e.explanation,
         "offer": offer, "offer_paise": off, "total_paise": e.net_paise + off}
    if not lean:
        o.update(reward_inr=e.reward_paise / 100, surcharge_inr=e.surcharge_paise / 100, net_inr=e.net_paise / 100,
                 effective_rate_pct=e.effective_rate_bps / 100, total_inr=(e.net_paise + off) / 100)
    return o


def usage_out(states: dict[str, CycleState]) -> dict:
    return {cid: {b: p / 100 for b, p in s.reward_by_bucket_paise.items()} for cid, s in states.items()}


def usage_paise_out(states: dict[str, CycleState]) -> dict:
    return {cid: dict(s.reward_by_bucket_paise) for cid, s in states.items()}


@dataclass
class Presenter:
    cfg: Settings
    offers: dict          # {"book": OfferBook}: a holder, so a reload swaps the book atomically

    def wallet_of(self, snap: CatalogSnapshot, ids: list[str], on: str | None = None):
        if len(ids) > self.cfg.max_wallet:
            raise HTTPException(422, f"wallet too large: {len(ids)} cards (max {self.cfg.max_wallet})")
        unknown = [c for c in ids if c not in snap.cards]
        if unknown:
            raise HTTPException(422, f"unknown cards: {unknown}")
        if on is not None:
            try:
                date.fromisoformat(on)
            except ValueError:
                raise HTTPException(422, "on must be a real date like 2026-11-07") from None
        return [card_on(snap.cards[c], on) for c in dict.fromkeys(ids)]   # a card listed twice is one card

    @staticmethod
    def day_of(txns) -> str | None:
        """The one date a multi-payment request is priced on. Mixed dates are refused rather than silently flattened."""
        days = {t.on for t in txns if t.on}
        if len(days) > 1:
            raise HTTPException(422, f"all payments in one request must share one date (got {sorted(days)}); send one request per date")
        return next(iter(days), None)

    def txn_of(self, t: TxnIn) -> Transaction:
        cap = self.cfg.max_amount_inr
        if t.amount_paise is None and t.amount is None:
            raise HTTPException(422, "amount_paise is required")
        if t.amount_paise is not None and t.amount is not None and _paise_from_rupees(t.amount) != t.amount_paise:
            raise HTTPException(422, "amount and amount_paise disagree; send only amount_paise")
        if t.amount_paise is not None:
            paise = t.amount_paise
        else:
            if not (t.amount <= cap):  # also rejects inf
                raise HTTPException(422, f"amount too large (max {cap:g} INR)")
            paise = _paise_from_rupees(t.amount)
        if paise > round(cap * 100):
            raise HTTPException(422, f"amount too large (max {cap:g} INR)")
        try:
            return Transaction(paise, t.mcc, t.channel, t.merchant)
        except ValueError as e:  # e.g. amount rounds to 0 paise
            raise HTTPException(422, str(e)) from None

    def usage_paise(self, usage_in: dict[str, dict[str, float]], usage_paise_in: dict[str, dict[str, int]] | None = None
                    ) -> dict[str, dict[str, int]]:
        cfg = self.cfg
        if usage_paise_in and usage_in:
            raise HTTPException(422, "send either cycle_usage_paise or the deprecated cycle_usage, not both")
        cap_paise = round(cfg.max_amount_inr * 100)
        usage = usage_paise_in or usage_in
        conv = (lambda v: v) if usage_paise_in else (lambda v: _paise_from_rupees(v) if math.isfinite(v) and abs(v) <= cfg.max_amount_inr else -1)   # range first: Decimal overflows on 1e300
        if len(usage) > cfg.max_usage_cards:
            raise HTTPException(422, f"cycle_usage covers too many cards (max {cfg.max_usage_cards})")
        result = {}
        for cid, buckets in usage.items():
            if len(buckets) > cfg.max_usage_buckets:
                raise HTTPException(422, f"cycle_usage for {cid!r} has too many buckets (max {cfg.max_usage_buckets})")
            row = {}
            for b, v in buckets.items():
                pv = conv(v)
                if not (0 <= pv <= cap_paise):  # rejects negatives, NaN and inf
                    raise HTTPException(422, f"cycle_usage[{cid!r}][{b!r}] must be between 0 and {cfg.max_amount_inr:g} INR")
                row[b] = pv
            result[cid] = row
        return result

    @staticmethod
    def states_of(usage: dict[str, dict[str, int]]) -> dict[str, CycleState]:
        return {cid: CycleState(dict(u)) for cid, u in usage.items()}

    @staticmethod
    def blocked(txn: Transaction) -> str | None:
        return block_reason(txn, (MERCHANTS.get(txn.merchant or "") or (None,))[0])

    def present(self, rec, txn: Transaction, include_offers: bool, explain: bool = True, lean: bool = False,
                rules: bool = False, offer_usage: dict[str, int] | None = None):
        """Shape a Recommendation for the wire. With offers on, cards are ranked by reward net of surcharge PLUS the
        best instant offer (same tie-breaks as the engine); otherwise it is the engine's own order, untouched."""
        book: OfferBook = self.offers["book"]
        applied: dict = {}
        why_not = self.blocked(txn) if rules else None
        if why_not:      # nothing can be paid this way: every card is shown as unavailable, with the reason
            outs = [out(Evaluation(e.card_id, e.card_name, False, 0, 0, 0, None, 0,
                                   [*e.explanation, why_not] if explain else (), False), None, lean, txn.amount_paise)
                    for e in rec.ranked]
            return None, outs
        ranked = rec.ranked
        if include_offers and book.offers:
            eligible = []
            for e in rec.ranked:
                if e.eligible:
                    e2, a = with_offer(e, book.best_for(e.card_id, txn, offer_usage))
                    applied[e.card_id] = a
                    eligible.append(e2)
            eligible.sort(key=lambda e: (-(e.net_paise + (applied[e.card_id].paise if applied[e.card_id] else 0)),
                                         e.uses_cap, e.card_id))
            ranked = eligible + [e for e in rec.ranked if not e.eligible]
        outs = []
        for e in ranked:
            a = applied.get(e.card_id)
            o = out(e, a.as_dict(lean) if a else None, lean, txn.amount_paise)
            if a and explain:
                o["why"] = [*e.explanation, f"Offer: {a.offer.label} → ₹{a.paise / 100:,.2f}"]
            outs.append(o)
        return (outs[0] if outs and outs[0]["eligible"] else None), outs
