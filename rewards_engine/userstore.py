"""Per-user state in SQLite: wallet, cycle cap usage, and the ledger of committed payments.

The engine stays a pure function; this is where a user's wallet and usage live so a refresh or retry does not lose them,
and where the ledger that reconciliation reads is kept. Opt-in by user_id.

* A commit is one transaction: the usage update and the ledger row land together or not at all.
* (user_id, txn_ref) is unique: retrying a commit returns the stored response and applies nothing twice.
* Usage is keyed by statement cycle (YYYY-MM), so a new month starts from zero.
* WAL journal and one process-wide write lock.
All money is integer paise.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

USER_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CYCLE = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])$")   # a real month, 2000-2099

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wallet (user_id TEXT NOT NULL, card_id TEXT NOT NULL, PRIMARY KEY (user_id, card_id));
CREATE TABLE IF NOT EXISTS usage (
  user_id TEXT NOT NULL, cycle TEXT NOT NULL, card_id TEXT NOT NULL, bucket TEXT NOT NULL,
  used_paise INTEGER NOT NULL CHECK (used_paise >= 0),
  PRIMARY KEY (user_id, cycle, card_id, bucket));
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, cycle TEXT NOT NULL, txn_ref TEXT NOT NULL,
  created_at TEXT NOT NULL, merchant TEXT, mcc TEXT, channel TEXT, amount_paise INTEGER NOT NULL,
  card_id TEXT NOT NULL, card_name TEXT, rule_id TEXT, expected_paise INTEGER NOT NULL,
  surcharge_paise INTEGER NOT NULL, net_paise INTEGER NOT NULL, offer_paise INTEGER NOT NULL DEFAULT 0,
  offer_id TEXT, left_on_table_paise INTEGER NOT NULL DEFAULT 0, rules_version TEXT,
  status TEXT NOT NULL DEFAULT 'pending', actual_paise INTEGER, credited_at TEXT, response TEXT NOT NULL, fingerprint TEXT,
  UNIQUE (user_id, txn_ref));
CREATE INDEX IF NOT EXISTS ledger_user_cycle ON ledger (user_id, cycle, id);
"""

LEDGER_COLS = ("txn_ref", "created_at", "cycle", "merchant", "mcc", "channel", "amount_paise", "card_id", "card_name",
               "rule_id", "expected_paise", "surcharge_paise", "net_paise", "offer_paise", "offer_id",
               "left_on_table_paise", "rules_version", "status", "actual_paise", "credited_at")


def current_cycle(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


class IdempotencyConflict(Exception):
    """The same (user, txn_ref) was already used for a different request."""


class UserStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(ledger)")}
            if "fingerprint" not in cols:            # databases created before request fingerprints existed
                self._db.execute("ALTER TABLE ledger ADD COLUMN fingerprint TEXT")

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def get_wallet(self, uid: str) -> list[str]:
        with self._lock:
            return [r["card_id"] for r in self._db.execute(
                "SELECT card_id FROM wallet WHERE user_id=? ORDER BY rowid", (uid,))]

    def get_usage(self, uid: str, cycle: str) -> dict[str, dict[str, int]]:
        with self._lock:
            return self._usage(self._db, uid, cycle)

    @staticmethod
    def _usage(db, uid: str, cycle: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in db.execute("SELECT card_id, bucket, used_paise FROM usage WHERE user_id=? AND cycle=?", (uid, cycle)):
            out.setdefault(r["card_id"], {})[r["bucket"]] = r["used_paise"]
        return out

    def get_offer_usage(self, uid: str, cycle: str) -> dict[str, int]:
        """offer id -> paise of discount this user has had from it in the cycle (for per-cycle offer limits)."""
        with self._lock:
            return {r["offer_id"]: r["used"] for r in self._db.execute(
                "SELECT offer_id, SUM(offer_paise) AS used FROM ledger WHERE user_id=? AND cycle=? AND offer_id IS NOT NULL GROUP BY offer_id",
                (uid, cycle))}

    def get_ledger(self, uid: str, cycle: str | None = None, limit: int = 500) -> list[dict]:
        q, args = f"SELECT {','.join(LEDGER_COLS)} FROM ledger WHERE user_id=?", [uid]
        if cycle:
            q += " AND cycle=?"; args.append(cycle)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args)]

    def has_user(self, uid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None

    @staticmethod
    def _ensure(db, uid: str) -> None:
        db.execute("INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
                   (uid, datetime.now(timezone.utc).isoformat()))

    def set_wallet(self, uid: str, cards: list[str]) -> None:
        with self._tx() as db:
            self._ensure(db, uid)
            db.execute("DELETE FROM wallet WHERE user_id=?", (uid,))
            db.executemany("INSERT OR IGNORE INTO wallet (user_id, card_id) VALUES (?, ?)", [(uid, c) for c in cards])

    def commit(self, uid: str, cycle: str, txn_ref: str,
               compute: Callable[[dict[str, dict[str, int]]], tuple[dict[str, dict[str, int]], dict, dict]]
               , fingerprint: str | None = None) -> tuple[dict, bool]:
        """Atomically apply one payment. `compute(usage)` returns (new_usage, ledger_row, response).

        Returns (response, replayed). If (uid, txn_ref) already exists, `compute` is NOT called and the
        stored response is returned with replayed=True, unless a `fingerprint` of the request was given and
        differs from the stored one: the same reference for a different payment is a client bug, so it raises
        IdempotencyConflict instead of silently returning the first payment's result. `ledger_row` needs the LEDGER_COLS fields it knows
        (status/actual/credited default); the response is stored verbatim for replays.
        """
        with self._tx() as db:
            self._ensure(db, uid)
            prev = db.execute("SELECT response, fingerprint FROM ledger WHERE user_id=? AND txn_ref=?", (uid, txn_ref)).fetchone()
            if prev is not None:
                if fingerprint and prev["fingerprint"] and prev["fingerprint"] != fingerprint:
                    raise IdempotencyConflict(txn_ref)
                return json.loads(prev["response"]), True
            usage = self._usage(db, uid, cycle)
            new_usage, row, response = compute(usage)
            for cid, buckets in new_usage.items():
                for b, v in buckets.items():
                    db.execute("INSERT INTO usage (user_id, cycle, card_id, bucket, used_paise) VALUES (?,?,?,?,?) "
                               "ON CONFLICT(user_id, cycle, card_id, bucket) DO UPDATE SET used_paise=excluded.used_paise",
                               (uid, cycle, cid, b, v))
            cols = [c for c in LEDGER_COLS if c in row]
            db.execute(f"INSERT INTO ledger (user_id, cycle, response, fingerprint, {','.join(cols)}) VALUES (?, ?, ?, ?, {','.join('?' * len(cols))})",
                       (uid, cycle, json.dumps(response, separators=(",", ":")), fingerprint, *[row[c] for c in cols]))
            return response, False

    def record_credits(self, uid: str, credits: list[dict]) -> tuple[int, list[str]]:
        """Store issuer credits against ledger rows (matched by txn_ref). Returns (rows updated, refs that matched nothing)."""
        n = 0
        unmatched: list[str] = []
        with self._tx() as db:
            for c in credits:
                cur = db.execute("UPDATE ledger SET actual_paise=?, credited_at=?, status='credited' WHERE user_id=? AND txn_ref=?",
                                 (c["actual_paise"], c.get("credited_at"), uid, c["txn_ref"]))
                n += cur.rowcount
                if not cur.rowcount:
                    unmatched.append(c["txn_ref"])
        return n, unmatched

    def reset(self, uid: str, cycle: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM usage WHERE user_id=? AND cycle=?", (uid, cycle))
            db.execute("DELETE FROM ledger WHERE user_id=? AND cycle=?", (uid, cycle))

    def close(self) -> None:
        with self._lock:
            self._db.close()
