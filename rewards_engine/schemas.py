"""Request bodies for the HTTP API. Money is integer paise; the rupee floats are deprecated (see presenter.py)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from .models import Channel

MAX_PAISE = 10**13        # Rs 100 billion: far above any real payment, far below SQLite's int64
Paise = Annotated[int, Field(ge=0, le=MAX_PAISE)]


class TxnIn(BaseModel):
    amount_paise: int | None = Field(None, gt=0, le=MAX_PAISE, description="integer paise (authoritative)")
    amount: float | None = Field(None, gt=0, description="INR; deprecated, use amount_paise")
    mcc: str = Field(pattern=r"^\d{4}$")
    channel: Channel
    merchant: str | None = Field(None, max_length=64)
    on: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO date the payment is made; rules are priced as they stood that day (default today)")


class OptimizeIn(BaseModel):
    transaction: TxnIn
    wallet: list[str] = Field(min_length=1, description="card ids the user holds")
    cycle_usage_paise: dict[str, dict[str, Paise]] = Field(
        default_factory=dict, description="card_id -> {cap_bucket: reward paise already earned this cycle}")
    cycle_usage: dict[str, dict[str, float]] = Field(
        default_factory=dict, description="deprecated INR version of cycle_usage_paise")
    include_offers: bool = Field(False, description="add instant offers (Checkout/online) to each card's value")
    offer_usage_paise: dict[str, Paise] = Field(default_factory=dict, description="offer id -> discount paise this user already had this cycle (per-cycle offer limits)")
    channel_rules: bool = Field(False, description="apply real channel rules (online-only merchants, the Rs 5,000 contactless limit) and add guidance")


class CommitIn(OptimizeIn):
    card_id: str | None = Field(None, description="card actually used; defaults to the best")
    user_id: str | None = Field(None, description="persist usage + ledger server-side for this user")
    txn_ref: str | None = Field(None, min_length=8, max_length=64, description="idempotency reference (with user_id)")
    cycle: str | None = Field(None, description="statement cycle YYYY-MM (default: current month)")


class BatchIn(BaseModel):
    transactions: list[TxnIn] = Field(min_length=1)
    wallet: list[str] = Field(min_length=1)
    cycle_usage_paise: dict[str, dict[str, Paise]] = Field(default_factory=dict, description="starting usage")
    cycle_usage: dict[str, dict[str, float]] = Field(default_factory=dict, description="deprecated INR version")
    sequential: bool = Field(False, description="apply each chosen card's cap consumption in order")
    explain: bool = Field(False, description="include the `why` trail (off by default for speed)")
    include_offers: bool = False
    offer_usage_paise: dict[str, Paise] = Field(default_factory=dict)
    channel_rules: bool = False


class PlanIn(BaseModel):
    transactions: list[TxnIn] = Field(min_length=1, description="the payments to plan, in order; the first is the one happening now")
    wallet: list[str] = Field(min_length=1)
    cycle_usage_paise: dict[str, dict[str, Paise]] = Field(default_factory=dict)
    user_id: str | None = Field(None, description="use this user's stored cap usage instead")
    include_offers: bool = True
    offer_usage_paise: dict[str, Paise] = Field(default_factory=dict)
    channel_rules: bool = True
    node_budget: int | None = Field(None, ge=100, le=500_000)


class UserPlanIn(BaseModel):
    transaction: TxnIn | None = Field(None, description="the payment happening now (optional); the rest is forecast from history")
    wallet: list[str] | None = Field(None, description="default: the user's saved wallet")
    include_offers: bool = True
    channel_rules: bool = True
    as_of: str | None = Field(None, description="ISO time to forecast from (default: now); for tests and what-ifs")


class WalletIn(BaseModel):
    cards: list[str] = Field(max_length=50)


class CreditIn(BaseModel):
    txn_ref: str = Field(min_length=1, max_length=64)
    actual_paise: Paise
    credited_at: str | None = Field(None, max_length=40)

    @field_validator("credited_at")
    @classmethod
    def _iso(cls, v):
        if v:
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("credited_at must be an ISO date or time") from None
        return v


class CreditsIn(BaseModel):
    credits: list[CreditIn] = Field(max_length=1000)


class ExpectedIn(BaseModel):
    txn_ref: str = Field(min_length=1, max_length=64)
    card_id: str
    amount_paise: int = Field(gt=0, le=MAX_PAISE)
    expected_paise: Paise
    created_at: str
    rule_id: str | None = None
    merchant: str | None = None


class ReconcileIn(BaseModel):
    expected: list[ExpectedIn] = Field(max_length=2000)
    credits: list[CreditIn] = Field(max_length=2000)
    settlement_days: int = Field(45, ge=1, le=365)
    as_of: str | None = Field(None, description="ISO time to evaluate 'overdue' against (default: now)")


class CompareIn(BaseModel):
    transaction: TxnIn
    wallet: list[str] = Field(min_length=1)
    offer_usage_paise: dict[str, Paise] = Field(default_factory=dict)


class PriceIn(BaseModel):
    item: str = Field(max_length=64)
    wallet: list[str] = Field(min_length=1)
    on: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
