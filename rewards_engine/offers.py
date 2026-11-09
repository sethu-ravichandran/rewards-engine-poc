"""Instant offers (bank offers, vouchers) on top of card rewards, kept outside the engine.

A card reward is credited later by the issuer and uses cycle caps; an offer is an instant discount with its own cap and
minimum order. Keeping them apart means the engine and its differential tests are untouched, the predicted reward is
exactly what the issuer should credit (so reconciliation compares like with like), and a stale offer file can only change
the discount. Value of a card = reward net of surcharge + the best applicable offer; one offer per card and payment.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .models import Channel, Transaction

DEFAULT_OFFERS_PATH = Path(__file__).resolve().parent.parent / "data" / "offers.json"


class OfferError(ValueError):
    """The offers file is structurally invalid."""


@dataclass(frozen=True, slots=True)
class Offer:
    id: str
    label: str
    kind: str
    merchants: frozenset[str]
    cards: frozenset[str]          # card ids, or {"*"} for any card
    channels: frozenset[Channel]
    discount_bps: int
    cap_paise: int | None          # per payment
    cycle_cap_paise: int | None    # per user per statement cycle (a real voucher is limited per month)
    min_txn_paise: int
    source: str | None
    verified_on: str | None
    confidence: str

    @property
    def replaces_reward(self) -> bool:
        """Buying a voucher is a prepaid/gift-card load for the issuer: it earns no card reward, so the voucher discount
        REPLACES the reward instead of stacking on it (issuers list wallet and gift-card loads as excluded)."""
        return self.kind == "voucher"

    def applies(self, card_id: str, txn: Transaction) -> bool:
        return (txn.channel in self.channels
                and (txn.merchant or "") in self.merchants
                and ("*" in self.cards or card_id in self.cards)
                and txn.amount_paise >= self.min_txn_paise)

    def value_paise(self, txn: Transaction) -> int:
        v = txn.amount_paise * self.discount_bps // 10_000   # floor, like card rewards
        return min(v, self.cap_paise) if self.cap_paise is not None else v


def with_offer(ev, a):
    """(evaluation, offer) as they really pay out. Normally unchanged. A voucher purchase earns no card reward, so the
    voucher is an ALTERNATIVE to the reward, never an addition: if the voucher beats the reward it replaces it (reward 0,
    no cap used); otherwise the voucher is dropped and the card pays its own reward. One rule shared by the optimizer
    view, the planner and commit so they cannot disagree."""
    if a is None or not a.offer.replaces_reward:
        return ev, a
    if a.paise <= ev.net_paise:
        return ev, None
    return dataclasses.replace(ev, reward_paise=0, net_paise=-ev.surcharge_paise, uses_cap=False, rule_id=None, effective_rate_bps=0,
                               explanation=(*ev.explanation, "Voucher purchase: the card earns no reward on it") if ev.explanation else ev.explanation), a


@dataclass(frozen=True, slots=True)
class AppliedOffer:
    offer: Offer
    paise: int

    def as_dict(self, lean: bool = False) -> dict:
        o = self.offer
        d = {"id": o.id, "label": o.label, "kind": o.kind, "paise": self.paise,
             "source": o.source, "verified_on": o.verified_on, "confidence": o.confidence}
        if not lean:
            d["inr"] = self.paise / 100          # deprecated display float for legacy clients
        return d


def _int(v, where: str, lo: int = 0, hi: int | None = None) -> int:
    if not (isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)):
        raise OfferError(f"{where}: must be an int in {lo}..{hi if hi is not None else 'inf'}")
    return v


def parse_offers(data: dict) -> tuple[Offer, ...]:
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        raise OfferError("root: must be an object with an 'offers' list")
    out, seen = [], set()
    for i, d in enumerate(data["offers"]):
        w = f"offers[{i}]"
        if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not d["id"]:
            raise OfferError(f"{w}: id required")
        if d["id"] in seen:
            raise OfferError(f"{w}: duplicate id {d['id']!r}")
        seen.add(d["id"])
        w = f"offer {d['id']!r}"
        for k in ("merchants", "cards", "channels"):
            if not (isinstance(d.get(k), list) and d[k] and all(isinstance(x, str) for x in d[k])):
                raise OfferError(f"{w}: {k} must be a non-empty list of strings")
        try:
            channels = frozenset(Channel(c) for c in d["channels"])
        except ValueError as e:
            raise OfferError(f"{w}: {e}") from None
        cap = d.get("cap_paise")
        out.append(Offer(
            id=d["id"], label=str(d.get("label") or d["id"]), kind=str(d.get("kind", "offer")),
            merchants=frozenset(d["merchants"]), cards=frozenset(d["cards"]), channels=channels,
            discount_bps=_int(d.get("discount_bps"), f"{w}.discount_bps", 1, 10_000),
            cap_paise=None if cap is None else _int(cap, f"{w}.cap_paise", 1),
            cycle_cap_paise=None if d.get("cycle_cap_paise") is None else _int(d["cycle_cap_paise"], f"{w}.cycle_cap_paise", 1),
            min_txn_paise=_int(d.get("min_txn_paise", 0), f"{w}.min_txn_paise"),
            source=d.get("source"), verified_on=d.get("verified_on"),
            confidence=str(d.get("confidence", "illustrative"))))
    return tuple(out)


class OfferBook:
    """Immutable set of offers with a content version (so caches keyed on it invalidate on change)."""

    def __init__(self, offers: tuple[Offer, ...], version: str) -> None:
        self.offers, self.version = offers, version
        by_merchant: dict[str, list[Offer]] = {}
        for o in offers:
            for m in o.merchants:
                by_merchant.setdefault(m, []).append(o)
        self._by_merchant = {m: tuple(v) for m, v in by_merchant.items()}

    @classmethod
    def from_data(cls, data: dict) -> "OfferBook":
        canon = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return cls(parse_offers(data), hashlib.sha256(canon.encode()).hexdigest()[:16])

    @classmethod
    def load(cls, path: Path | str = DEFAULT_OFFERS_PATH) -> "OfferBook":
        p = Path(path)
        if not p.exists():
            return cls((), "none")
        return cls.from_data(json.loads(p.read_text()))

    def best_for(self, card_id: str, txn: Transaction, used: dict[str, int] | None = None) -> AppliedOffer | None:
        """The single best applicable offer for this card and payment, or None.

        `used` is what this user has already had from each offer this cycle (offer id -> paise): an offer with a
        per-cycle limit stops paying once the limit is reached, so repeated payments do not overstate the savings."""
        best: AppliedOffer | None = None
        for o in self._by_merchant.get(txn.merchant or "", ()):
            if o.applies(card_id, txn):
                v = o.value_paise(txn)
                if o.cycle_cap_paise is not None:
                    v = min(v, max(0, o.cycle_cap_paise - (used or {}).get(o.id, 0)))
                # ties: earliest declared (stable), so results are deterministic
                if v > 0 and (best is None or v > best.paise):
                    best = AppliedOffer(o, v)
        return best

    def value_paise(self, card_id: str, txn: Transaction, used: dict[str, int] | None = None) -> int:
        a = self.best_for(card_id, txn, used)
        return a.paise if a else 0


EMPTY = OfferBook((), "none")
