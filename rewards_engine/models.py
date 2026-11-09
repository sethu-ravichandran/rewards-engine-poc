"""Core domain types for the rewards optimizer.

Money is handled in integer paise (1 INR = 100 paise) to avoid float drift.
Reward values are computed in paise too, with explicit rounding rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class Channel(str, Enum):
    """How the payment is made. The four ways to pay."""
    ONLINE = "online"      # Checkout route: card-not-present
    CARD = "card"          # Card route: POS swipe / dip
    TAP = "tap"            # Tap route: contactless NFC
    UPI = "upi"            # UPI route: RuPay credit card on UPI


class Network(str, Enum):
    VISA = "visa"
    MASTERCARD = "mastercard"
    RUPAY = "rupay"
    AMEX = "amex"
    DINERS = "diners"


@dataclass(frozen=True, slots=True)
class Transaction:
    amount_paise: int
    mcc: str
    channel: Channel
    merchant: str | None = None  # normalized merchant id, e.g. "amazon", "swiggy"

    def __post_init__(self) -> None:
        if self.amount_paise <= 0:
            raise ValueError("amount must be positive")
        if not (len(self.mcc) == 4 and self.mcc.isdigit()):
            raise ValueError(f"invalid MCC: {self.mcc!r}")


@dataclass(frozen=True, slots=True)
class EarnRule:
    """A reward rule. The most specific matching rule wins (see engine).

    rate_bps: reward earned, in basis points of spend, *in the card's reward unit
      value*. E.g. 5% cashback = 500 bps. For points cards, we express it as
      effective value: points_per_100 * point_value. Kept as bps so all cards
      compare in rupees.
    cap_paise: max reward value per statement cycle for this rule's cap bucket.
    cap_bucket: rules sharing a bucket share one cap (e.g. "accelerated").
    """
    id: str
    rate_bps: int
    mccs: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    merchants: frozenset[str] = frozenset()
    channels: frozenset[Channel] = frozenset()
    cap_paise: int | None = None
    cap_bucket: str | None = None
    min_txn_paise: int = 0
    # ISO dates (YYYY-MM-DD): the rule applies from `effective_from` (inclusive) until `effective_to` (exclusive).
    # Applied by catalog.card_on() BEFORE the engine runs; the engine itself never looks at dates.
    effective_from: str | None = None
    effective_to: str | None = None

    @property
    def specificity(self) -> int:
        # merchant > mcc > category > channel-only > base
        return (
            (8 if self.merchants else 0)
            + (4 if self.mccs else 0)
            + (2 if self.categories else 0)
            + (1 if self.channels else 0)
        )


class CardIndex:
    """Per-card rule index, built once when the Card is constructed (i.e. at catalog load).

    A rule can only match a transaction whose merchant / MCC / category it names, so rules are
    bucketed by their most selective constraint and a lookup touches only the few rules that could
    possibly match instead of scanning all of them. Each entry carries a precomputed ranking key
    (specificity, rate_bps, -declaration_order): the max key reproduces the original
    `max(candidates, key=(specificity, rate_bps))`, whose ties went to the earliest-declared rule.
    """
    __slots__ = ("by_merchant", "by_mcc", "by_category", "general", "rule_by_id", "has_rules")

    def __init__(self, rules: Sequence["EarnRule"]) -> None:
        by_m: dict[str, list] = {}
        by_mcc: dict[str, list] = {}
        by_cat: dict[str, list] = {}
        general: list = []
        self.rule_by_id: dict[str, EarnRule] = {}
        for order, r in enumerate(rules):
            self.rule_by_id.setdefault(r.id, r)  # first declaration wins, as `next(...)` did
            entry = ((r.specificity, r.rate_bps, -order), r)
            if r.merchants:
                for m in r.merchants:
                    by_m.setdefault(m, []).append(entry)
            elif r.mccs:
                for m in r.mccs:
                    by_mcc.setdefault(m, []).append(entry)
            elif r.categories:
                for c in r.categories:
                    by_cat.setdefault(c, []).append(entry)
            else:
                general.append(entry)
        self.by_merchant = {k: tuple(v) for k, v in by_m.items()}
        self.by_mcc = {k: tuple(v) for k, v in by_mcc.items()}
        self.by_category = {k: tuple(v) for k, v in by_cat.items()}
        self.general = tuple(general)
        self.has_rules = bool(rules)


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    network: Network
    base_rate_bps: int
    rules: tuple[EarnRule, ...] = ()
    excluded_categories: frozenset[str] = frozenset()
    excluded_mccs: frozenset[str] = frozenset()
    # surcharges the user pays on this card, in bps of txn (e.g. fuel 1%)
    surcharge_bps: dict[str, int] = field(default_factory=dict)
    surcharge_waiver: dict[str, tuple[int, int]] = field(default_factory=dict)
    # category -> (min_txn_paise, max_txn_paise) where surcharge is waived
    supported_channels: frozenset[Channel] = frozenset(
        {Channel.ONLINE, Channel.CARD, Channel.TAP}
    )
    available_limit_paise: int | None = None
    # provenance for the rules (source, verified_on, confidence); never read by the engine
    meta: dict = field(default_factory=dict, compare=False, repr=False)
    index: CardIndex = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # frozen dataclass: bypass __setattr__. Rebuilt by dataclasses.replace() too.
        object.__setattr__(self, "index", CardIndex(self.rules))


@dataclass
class CycleState:
    """Per-card usage in the current statement cycle (caps consumed)."""
    reward_by_bucket_paise: dict[str, int] = field(default_factory=dict)

    def used(self, bucket: str) -> int:
        return self.reward_by_bucket_paise.get(bucket, 0)

    def add(self, bucket: str, paise: int) -> None:
        self.reward_by_bucket_paise[bucket] = self.used(bucket) + paise


@dataclass(slots=True)
class Evaluation:
    # Not frozen: a frozen dataclass is ~4x slower to construct and one is built per card per request.
    # Treat instances as immutable.
    card_id: str
    card_name: str
    eligible: bool
    reward_paise: int
    surcharge_paise: int
    net_paise: int
    rule_id: str | None
    effective_rate_bps: int
    explanation: Sequence[str]  # empty tuple when evaluated with explain=False
    uses_cap: bool = False  # reward came from a capped rule (consumes scarce cap)
