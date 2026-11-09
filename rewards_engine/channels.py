"""Channel rules and plain-language guidance, applied on top of the engine (opt-in via channel_rules).

* Online-only merchants (Amazon, Swiggy) cannot be tapped or swiped.
* Contactless payments above Rs 5,000 need a PIN, so they are Card payments, not Tap.

guidance() turns a losing result into advice: split a petrol fill that is just above the surcharge-waiver band, or pay
rent from a bank account instead of a card. All money is integer paise.
"""
from __future__ import annotations

import math

from .mcc import category_for
from .models import Card, Channel, Transaction

# merchants with no physical counter: Card (swipe/dip) and Tap (contactless) cannot be used there
ONLINE_ONLY = frozenset({"amazon", "flipkart", "myntra", "zomato", "swiggy", "bigbasket", "uber", "airtel",
                         "bescom", "lic", "smartbuy_hotels"})
TAP_LIMIT_PAISE = 500_000          # contactless without a PIN is capped at Rs 5,000 per payment


def block_reason(txn: Transaction, merchant_name: str | None = None, tap_limit: int = TAP_LIMIT_PAISE) -> str | None:
    """Why this payment cannot be made on this channel at all, or None if it can."""
    if txn.channel in (Channel.CARD, Channel.TAP) and (txn.merchant or "") in ONLINE_ONLY:
        name = merchant_name or txn.merchant
        return f"{name} is online-only, so there is nothing to tap or swipe: use Checkout."
    if txn.channel == Channel.TAP and txn.amount_paise > tap_limit:
        return f"Contactless payments above ₹{tap_limit / 100:,.0f} need a PIN: use Card for this one."
    return None


def _rs(paise: int) -> str:
    return f"₹{paise / 100:,.0f}"


def guidance(wallet: list[Card], txn: Transaction, best_total_paise: int | None, prefer_card_id: str | None = None) -> list[str]:
    """Advice for a payment that costs money on every card, in plain language. Empty when there is nothing to add.

    `prefer_card_id` is the card the screen is recommending: advice about a surcharge waiver is about that card
    when it has one, so the advice and the recommendation never name different cards."""
    if best_total_paise is None or best_total_paise >= 0:
        return []
    cat = category_for(txn.mcc)
    label = cat.replace("_", " ")
    cards = [c for c in wallet if cat in c.surcharge_bps]
    if not cards:
        return []
    cards.sort(key=lambda c: c.id != prefer_card_id)              # the recommended card first (sort is stable)
    tips: list[tuple[int, int, str]] = []
    for rank, c in enumerate(cards):
        w = c.surcharge_waiver.get(cat)
        if not w:
            continue
        lo, hi = w
        if txn.amount_paise > hi and hi > 0:
            n = math.ceil(txn.amount_paise / hi)
            part = math.ceil(txn.amount_paise / n)
            if part >= lo:
                tips.append((n, rank, f"Split {_rs(txn.amount_paise)} into {n} payments of about {_rs(part)}: the {label} "
                                      f"surcharge on {c.name} is waived between {_rs(lo)} and {_rs(hi)} per payment."))
        elif txn.amount_paise < lo:
            fee = txn.amount_paise * c.surcharge_bps[cat] // 10_000
            tips.append((1, rank, f"The fee here is only ₹{fee / 100:,.2f}. The {label} surcharge on {c.name} "
                                  f"is waived on single payments of {_rs(lo)} to {_rs(hi)}, so it only matters for a small top-up."))
    if tips:
        return [min(tips, key=lambda t: (t[0], t[1]))[2]]
    pct = max(c.surcharge_bps[cat] for c in cards) / 100
    return [f"Cards charge {pct:g}% on {label} and earn nothing on it. Paying from your bank account avoids the fee if the payee accepts it: "
            "a bank transfer, or UPI from the account rather than the credit card (a credit card on UPI is still a card payment)."]
