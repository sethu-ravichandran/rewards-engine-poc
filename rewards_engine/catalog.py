"""Card rules loaded from JSON, plus the rules lifecycle.

* validate_catalog checks the JSON structure before anything is built from it.
* rules_version is a content hash of the parsed JSON.
* CatalogSnapshot is an immutable view (cards, version, pre-rendered /api/meta body). A request takes one snapshot and
  keeps it, so a hot reload is never seen half-applied.
* CatalogStore swaps snapshots atomically when the file changes, and keeps serving the old one if the new file is invalid.
* card_on gives a card as it priced payments on a date, using each rule's effective_from / effective_to.
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .channels import ONLINE_ONLY, TAP_LIMIT_PAISE
from .mcc import MCC_CATEGORIES, category_for
from .models import Card, Channel, EarnRule, Network

log = logging.getLogger("rewards_engine.catalog")

DEFAULT_PATH = Path(os.environ.get("RULES_PATH") or Path(__file__).resolve().parent.parent / "data" / "cards.json")

# merchant id -> (display name, MCC). Drives the UI presets.
MERCHANTS: dict[str, tuple[str, str]] = {
    "amazon": ("Amazon", "5311"),
    "swiggy": ("Swiggy", "5814"),
    "zomato": ("Zomato", "5814"),
    "bigbasket": ("BigBasket", "5411"),
    "croma": ("Croma", "5732"),
    "flipkart": ("Flipkart", "5311"),
    "myntra": ("Myntra", "5651"),
    "uber": ("Uber", "4121"),
    "indian_oil": ("Petrol pump", "5541"),
    "dmart": ("DMart", "5411"),
    "airtel": ("Mobile bill", "4814"),
    "bescom": ("Electricity bill", "4900"),
    "rent": ("Rent", "6513"),
    "lic": ("LIC premium", "6300"),
    "smartbuy_hotels": ("SmartBuy hotels", "7011"),
    "apollo": ("Pharmacy", "5912"),
    "local_kirana": ("Kirana store", "5499"),
}


# provenance of a card's rules; validated and surfaced, never used by the engine
PROVENANCE_KEYS = ("source", "source_note", "verified_on", "confidence")
CONFIDENCE = ("verified", "reviewed", "illustrative")


def _rule(d: dict) -> EarnRule:
    return EarnRule(
        id=d["id"],
        rate_bps=int(d["rate_bps"]),
        mccs=frozenset(d.get("mccs", [])),
        categories=frozenset(d.get("categories", [])),
        merchants=frozenset(d.get("merchants", [])),
        channels=frozenset(Channel(c) for c in d.get("channels", [])),
        cap_paise=d.get("cap_paise"),
        cap_bucket=d.get("cap_bucket"),
        min_txn_paise=int(d.get("min_txn_paise", 0)),
        effective_from=d.get("effective_from"),
        effective_to=d.get("effective_to"),
    )


def card_on(card: Card, day: str | None) -> Card:
    """The card as it priced payments on `day` (ISO date; default today): rules outside their window are dropped.
    A card with no dated rules is returned as is, so the common path costs one attribute scan."""
    if not any(r.effective_from or r.effective_to for r in card.rules):
        return card
    d = day or date.today().isoformat()
    live = tuple(r for r in card.rules if (r.effective_from or "") <= d and (not r.effective_to or d < r.effective_to))
    return dataclasses.replace(card, rules=live)


def card_from_dict(d: dict) -> Card:
    kwargs = {}
    if "supported_channels" in d:
        kwargs["supported_channels"] = frozenset(Channel(c) for c in d["supported_channels"])
    return Card(
        id=d["id"],
        name=d["name"],
        network=Network(d["network"]),
        base_rate_bps=int(d["base_rate_bps"]),
        rules=tuple(_rule(r) for r in d.get("rules", [])),
        excluded_categories=frozenset(d.get("excluded_categories", [])),
        excluded_mccs=frozenset(d.get("excluded_mccs", [])),
        surcharge_bps=dict(d.get("surcharge_bps", {})),
        surcharge_waiver={k: tuple(v) for k, v in d.get("surcharge_waiver", {}).items()},
        available_limit_paise=d.get("available_limit_paise"),
        meta={k: d.get(k) for k in PROVENANCE_KEYS if k in d},
        **kwargs,
    )


def load_cards(path: Path | str = DEFAULT_PATH) -> dict[str, Card]:
    data = json.loads(Path(path).read_text())
    return {c.id: c for c in map(card_from_dict, data["cards"])}


class CatalogError(ValueError):
    """The rules file is structurally invalid."""


_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")  # ids end up in cache keys: no delimiters allowed
_CHANNELS = {c.value for c in Channel}
_NETWORKS = {n.value for n in Network}


def _need(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise CatalogError(f"{where}: {msg}")


def _is_day(v) -> bool:
    """A real ISO calendar date, exactly YYYY-MM-DD (no trailing text, no 2026-13-45)."""
    if not (isinstance(v, str) and len(v) == 10):
        return False
    try:
        return date.fromisoformat(v).isoformat() == v
    except ValueError:
        return False


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_list(v, where: str, pattern: re.Pattern | None = None) -> None:
    _need(isinstance(v, list) and all(isinstance(x, str) for x in v), where, "must be a list of strings")
    if pattern:
        for x in v:
            _need(bool(pattern.match(x)), where, f"bad value {x!r}")


def validate_catalog(data) -> None:
    """Raise CatalogError unless `data` has the shape load_cards() relies on."""
    _need(isinstance(data, dict), "root", "must be an object")
    cards = data.get("cards")
    _need(isinstance(cards, list) and cards, "cards", "must be a non-empty list")
    seen: set[str] = set()
    for i, c in enumerate(cards):
        w = f"cards[{i}]"
        _need(isinstance(c, dict), w, "must be an object")
        _need(isinstance(c.get("id"), str) and bool(_ID.match(c["id"])), w, "id must match [A-Za-z0-9_.:-]{1,64}")
        w = f"card '{c['id']}'"
        _need(c["id"] not in seen, w, "duplicate id")
        seen.add(c["id"])
        _need(isinstance(c.get("name"), str) and c["name"], w, "name required")
        _need(c.get("network") in _NETWORKS, w, f"network must be one of {sorted(_NETWORKS)}")
        _need(_is_int(c.get("base_rate_bps")) and 0 <= c["base_rate_bps"] <= 10_000, w, "base_rate_bps must be int in 0..10000")
        if "supported_channels" in c:
            _str_list(c["supported_channels"], w + ".supported_channels")
            _need(set(c["supported_channels"]) <= _CHANNELS and c["supported_channels"], w, "bad supported_channels")
        for k in ("excluded_categories", "excluded_mccs"):
            if k in c:
                _str_list(c[k], f"{w}.{k}")
        if "display_order" in c:
            _need(_is_int(c["display_order"]), w, "display_order must be an int")
        if "confidence" in c:
            _need(c["confidence"] in CONFIDENCE, w, f"confidence must be one of {list(CONFIDENCE)}")
        if c.get("verified_on") is not None:
            _need(_is_day(c["verified_on"]), w, "verified_on must be a real date, YYYY-MM-DD, or null")
        for k in ("source", "source_note"):
            if c.get(k) is not None:
                _need(isinstance(c[k], str) and len(c[k]) <= 500, w, f"{k} must be a string (max 500 chars)")
        if "available_limit_paise" in c and c["available_limit_paise"] is not None:
            _need(_is_int(c["available_limit_paise"]) and c["available_limit_paise"] >= 0, w, "available_limit_paise must be int >= 0")
        sb = c.get("surcharge_bps", {})
        _need(isinstance(sb, dict) and all(_is_int(v) and 0 <= v <= 10_000 for v in sb.values()), w, "surcharge_bps must map category -> int bps")
        sw = c.get("surcharge_waiver", {})
        _need(isinstance(sw, dict) and all(isinstance(v, list) and len(v) == 2 and all(_is_int(x) for x in v)
                                           for v in sw.values()), w, "surcharge_waiver must map category -> [lo, hi]")
        rules = c.get("rules", [])
        _need(isinstance(rules, list), w, "rules must be a list")
        rids: set[str] = set()
        for j, r in enumerate(rules):
            rw = f"{w} rules[{j}]"
            _need(isinstance(r, dict), rw, "must be an object")
            _need(isinstance(r.get("id"), str) and r["id"], rw, "id required")
            rw = f"{w} rule '{r['id']}'"
            for k in ("effective_from", "effective_to"):
                if r.get(k) is not None:
                    _need(_is_day(r[k]), rw, f"{k} must be a real date, YYYY-MM-DD")
            _need(not (r.get("effective_from") and r.get("effective_to") and r["effective_from"] >= r["effective_to"]),
                  rw, "effective_from must be before effective_to")
            # the same rule id may appear again only as a dated version whose window does not overlap the others
            same = [x for x in rules[:j] if x.get("id") == r["id"]]
            for x in same:
                lo, hi = r.get("effective_from") or "", r.get("effective_to") or "9999-12-31"
                xlo, xhi = x.get("effective_from") or "", x.get("effective_to") or "9999-12-31"
                _need(bool(x.get("effective_from") or x.get("effective_to")) and bool(r.get("effective_from") or r.get("effective_to"))
                      and (hi <= xlo or xhi <= lo), rw, "duplicate rule id (allowed only for dated versions whose windows do not overlap)")
            rids.add(r["id"])
            _need(_is_int(r.get("rate_bps")) and 0 <= r["rate_bps"] <= 10_000, rw, "rate_bps must be int in 0..10000")
            for k in ("merchants", "categories"):
                if k in r:
                    _str_list(r[k], f"{rw}.{k}")
            if "mccs" in r:
                _str_list(r["mccs"], f"{rw}.mccs", re.compile(r"^\d{4}$"))
            if "channels" in r:
                _str_list(r["channels"], f"{rw}.channels")
                _need(set(r["channels"]) <= _CHANNELS, rw, "unknown channel")
            if r.get("cap_paise") is not None:
                _need(_is_int(r["cap_paise"]) and r["cap_paise"] > 0, rw, "cap_paise must be a positive int")
            if r.get("cap_bucket") is not None:
                _need(isinstance(r["cap_bucket"], str) and bool(_ID.match(r["cap_bucket"])), rw, "bad cap_bucket")
            if "min_txn_paise" in r:
                _need(_is_int(r["min_txn_paise"]) and r["min_txn_paise"] >= 0, rw, "min_txn_paise must be int >= 0")


def rules_version(data) -> str:
    """Content hash of the parsed rules: stable across whitespace / key order, changes with any rule."""
    canon = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _cap_buckets(card: Card) -> tuple[tuple[str, int], ...]:
    """(bucket, largest cap sharing it): the only cycle-usage values that can change an answer."""
    caps: dict[str, int] = {}
    for r in card.rules:
        if r.cap_paise is not None:
            b = r.cap_bucket or r.id
            caps[b] = max(caps.get(b, 0), r.cap_paise)
    return tuple(sorted(caps.items()))


def _meta_payload(cards: dict[str, Card], version: str) -> dict:
    return {
        "cards": [
            {"id": c.id, "name": c.name, "network": c.network.value,
             "base_rate_pct": c.base_rate_bps / 100,
             "channels": sorted(ch.value for ch in c.supported_channels),
             "caps": {(r.cap_bucket or r.id): r.cap_paise / 100 for r in c.rules if r.cap_paise},
             "caps_paise": {(r.cap_bucket or r.id): r.cap_paise for r in c.rules if r.cap_paise},
             "provenance": {"source": c.meta.get("source"), "verified_on": c.meta.get("verified_on"),
                            "confidence": c.meta.get("confidence", "illustrative"),
                            "source_note": c.meta.get("source_note")}}
            for c in cards.values()
        ],
        "merchants": [{"id": k, "name": v[0], "mcc": v[1], "category": category_for(v[1]), "online_only": k in ONLINE_ONLY}
                      for k, v in MERCHANTS.items()],
        "tap_limit_paise": TAP_LIMIT_PAISE,
        "mccs": [{"mcc": m, "category": c} for m, c in sorted(MCC_CATEGORIES.items())],
        "rules_version": version,
    }


@dataclass(frozen=True)
class CatalogSnapshot:
    """Immutable, fully pre-computed view of one version of the rules."""
    version: str
    cards: dict[str, Card]
    cap_buckets: dict[str, tuple[tuple[str, int], ...]]
    meta_body: bytes
    meta_gzip: bytes
    meta_etag: str
    loaded_at: float

    @classmethod
    def from_data(cls, data: dict) -> "CatalogSnapshot":
        validate_catalog(data)
        version = rules_version(data)
        try:
            cards = {c.id: c for c in map(card_from_dict, data["cards"])}
        except (KeyError, ValueError, TypeError) as e:  # defence in depth beyond validate_catalog
            raise CatalogError(f"cannot build cards: {e}") from e
        body = json.dumps(_meta_payload(cards, version), ensure_ascii=False, separators=(",", ":")).encode()
        return cls(version, cards, {cid: _cap_buckets(c) for cid, c in cards.items()},
                   body, gzip.compress(body, 6, mtime=0),
                   '"' + hashlib.sha256(body).hexdigest()[:32] + '"', time.time())


class CatalogStore:
    """Holds the current snapshot; reloads it atomically when the rules file changes."""

    def __init__(self, path: Path | str = DEFAULT_PATH, watch_interval_s: float = 2.0) -> None:
        self.path = Path(path)
        self.watch_interval_s = watch_interval_s
        self._lock = threading.Lock()
        self._sig: tuple[int, int] | None = None
        self._bad_sig: tuple[int, int] | None = None  # avoid re-logging the same broken file
        self._next_check = 0.0
        self.last_error: str | None = None
        self._snap = self._read()  # fail fast at startup: no snapshot, no service
        self._next_check = time.monotonic() + watch_interval_s

    def _stat(self) -> tuple[int, int]:
        st = self.path.stat()
        return (st.st_mtime_ns, st.st_size)

    def _read(self) -> CatalogSnapshot:
        sig = self._stat()
        snap = CatalogSnapshot.from_data(json.loads(self.path.read_text()))
        self._sig = sig
        return snap

    def get(self) -> CatalogSnapshot:
        """Current snapshot. Cheap: at most one stat() per watch interval, by one thread."""
        if self.watch_interval_s > 0 and time.monotonic() >= self._next_check:
            if self._lock.acquire(blocking=False):
                try:
                    self._next_check = time.monotonic() + self.watch_interval_s
                    self._reload_locked(force=False)
                finally:
                    self._lock.release()
        return self._snap

    def reload(self, force: bool = True) -> str:
        """Re-read the file now. Returns 'reloaded' | 'unchanged'; raises CatalogError if invalid."""
        with self._lock:
            return self._reload_locked(force, raise_on_error=True)

    def _reload_locked(self, force: bool, raise_on_error: bool = False) -> str:
        try:
            sig = self._stat()
            if not force and (sig == self._sig or sig == self._bad_sig):
                return "unchanged"
            data = json.loads(self.path.read_text())
            if rules_version(data) == self._snap.version and not force:
                self._sig = sig
                return "unchanged"
            new = CatalogSnapshot.from_data(data)
        except (OSError, ValueError) as e:  # json errors and CatalogError are ValueErrors
            msg = f"{type(e).__name__}: {e}"
            self.last_error = msg
            try:
                self._bad_sig = self._stat()
            except OSError:
                pass
            log.error("rules reload rejected, keeping version %s: %s", self._snap.version, msg)
            if raise_on_error:
                raise CatalogError(msg) from e
            return "invalid"
        self._sig, self._bad_sig, self.last_error = sig, None, None
        if new.version == self._snap.version:
            return "unchanged"
        old, self._snap = self._snap.version, new  # the atomic swap
        log.info("rules reloaded: %s -> %s", old, new.version)
        return "reloaded"

