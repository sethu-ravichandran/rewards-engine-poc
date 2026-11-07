"""Runtime settings, read from environment variables in one place."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


@dataclass(frozen=True)
class Settings:
    # rules lifecycle
    rules_path: str = ""                   # RULES_PATH (default: data/cards.json)
    rules_watch_interval_s: float = 2.0    # RULES_WATCH_INTERVAL_S; 0 disables file watching
    admin_token: str = ""                  # ADMIN_TOKEN; empty disables POST /api/admin/reload
    # /api/meta HTTP caching
    meta_max_age_s: int = 30               # META_MAX_AGE_S (Cache-Control max-age for /api/meta)
    gzip_min_bytes: int = 1024             # GZIP_MIN_BYTES
    # offers, planner, per-user store
    offers_path: str = ""                  # OFFERS_PATH (default: data/offers.json)
    user_db: str = ""                      # USER_DB: SQLite file for wallets/usage/ledger (default: data/users.sqlite3; ':memory:' ok)
    stateless: bool = False                # STATELESS: no per-user storage; the UI keeps its own state (set automatically on Vercel)
    plan_max_txns: int = 30                # PLAN_MAX_TXNS
    plan_node_budget: int = 150_000        # PLAN_NODE_BUDGET (search nodes before the planner returns its best so far)
    # request limits
    max_batch: int = 200                   # MAX_BATCH
    max_wallet: int = 20                   # MAX_WALLET
    max_amount_inr: float = 100_000_000.0  # MAX_AMOUNT_INR (10 crore); also bounds cycle usage values
    max_usage_cards: int = 50              # MAX_USAGE_CARDS
    max_usage_buckets: int = 20            # MAX_USAGE_BUCKETS (per card)
    max_body_bytes: int = 1_000_000        # MAX_BODY_BYTES (checked against Content-Length)
    # observability
    log_level: str = "INFO"                # LOG_LEVEL
    access_log: bool = True                # ACCESS_LOG

    @classmethod
    def from_env(cls) -> "Settings":
        kw = {}
        for f in fields(cls):
            t = {"int": int, "float": float, "bool": bool, "str": str}[f.type if isinstance(f.type, str) else f.type.__name__]
            kw[f.name] = _env(f.name.upper(), f.default, t)
        return cls(**kw)
