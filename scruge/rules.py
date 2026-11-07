"""Rules pipeline: get card rules in, reviewed, live, and back out. CLI: python -m scruge.rules ...

  build DIR           merge one-file-per-card (DIR/*.json) into one rules file
  lint FILE           structure errors; provenance and sanity warnings
  diff OLD NEW        what changed per card, and which changes need a human
  publish NEW         lint, diff against live, refuse on errors or large changes (unless --approve-large),
                      archive, swap atomically, log. A running service picks it up on hot reload.
  rollback [VERSION]  restore a previous release (default: the one before the current)
  history             the audit trail

Releases are stored as releases/<version>.json (version = content hash) and every action is appended to
releases/history.jsonl. Exit codes: 0 ok, 1 usage/IO, 2 invalid rules, 3 needs approval.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .catalog import CatalogError, rules_version, validate_catalog

DATA = Path(__file__).resolve().parent.parent / "data"
LIVE = DATA / "cards.json"
RELEASES = DATA / "releases"

STALE_AFTER_DAYS = 90
LARGE_RATE_CHANGE_BPS = 500      # a 5-point swing in any rate needs a human
SUSPICIOUS_RATE_BPS = 3000       # >30% effective is almost certainly a typo (points vs rupees)


class PublishRefused(Exception):
    def __init__(self, msg: str, code: int) -> None:
        super().__init__(msg)
        self.code = code


def build_from_dir(directory: Path | str, note: str = "") -> dict:
    files = sorted(Path(directory).glob("*.json"))
    if not files:
        raise PublishRefused(f"no *.json card files in {directory}", 1)
    cards = []
    for f in files:
        try:
            c = json.loads(f.read_text())
        except ValueError as e:
            raise PublishRefused(f"{f.name}: invalid JSON: {e}", 2) from None
        if not isinstance(c, dict) or "id" not in c:
            raise PublishRefused(f"{f.name}: must be one card object with an id", 2)
        if f.stem != c["id"]:
            raise PublishRefused(f"{f.name}: file name must match the card id {c['id']!r}", 2)
        cards.append(c)
    cards.sort(key=lambda c: (c.get("display_order", 1000), c["id"]))   # display_order keeps the UI order stable; ties by id
    out = {"cards": cards}
    if note:
        out["_note"] = note
    return out


def lint(data: dict, today: date | None = None) -> list[dict]:
    """Issues as {level: error|warn|info, card, msg}. Errors block publishing."""
    today = today or date.today()
    issues: list[dict] = []

    def add(level: str, card: str | None, msg: str) -> None:
        issues.append({"level": level, "card": card, "msg": msg})

    try:
        validate_catalog(data)
    except CatalogError as e:
        add("error", None, str(e))
        return issues
    names: dict[str, str] = {}
    for c in data["cards"]:
        cid = c["id"]
        if not c.get("source"):
            add("warn", cid, "no source URL recorded for these rules")
        for r in c.get("rules", []):
            if r.get("effective_from") and r["effective_from"] > today.isoformat():
                add("info", cid, f"rule '{r['id']}' is scheduled: not live until {r['effective_from']}")
            if r.get("effective_to") and r["effective_to"] <= today.isoformat():
                add("warn", cid, f"rule '{r['id']}' expired on {r['effective_to']}: remove it or confirm it is kept for history")
        conf = c.get("confidence", "illustrative")
        if conf == "illustrative":
            add("info", cid, "rules are illustrative, not read from the issuer's T&Cs")
        if conf in ("verified", "reviewed"):
            v = c.get("verified_on")
            if not v:
                add("error", cid, f"confidence '{conf}' requires verified_on")
            else:
                age = (today - date.fromisoformat(v)).days
                if age < 0:
                    add("error", cid, f"verified_on {v} is in the future")
                elif age > STALE_AFTER_DAYS:
                    add("warn", cid, f"last verified {age} days ago (limit {STALE_AFTER_DAYS})")
        nm = c["name"].strip().lower()
        if nm in names:
            add("warn", cid, f"same name as card '{names[nm]}'")
        names[nm] = cid
        if c["base_rate_bps"] > 1000:
            add("warn", cid, f"base rate {c['base_rate_bps'] / 100:.2f}% looks high")
        caps: dict[str, set[int]] = {}
        for r in c.get("rules", []):
            if r["rate_bps"] > SUSPICIOUS_RATE_BPS:
                add("warn", cid, f"rule '{r['id']}' pays {r['rate_bps'] / 100:.2f}%: points vs rupees mix-up?")
            if r["rate_bps"] < c["base_rate_bps"]:
                add("info", cid, f"rule '{r['id']}' ({r['rate_bps'] / 100:.2f}%) is below the base rate: the base rate will apply")
            if r.get("cap_paise"):
                caps.setdefault(r.get("cap_bucket") or r["id"], set()).add(r["cap_paise"])
        for b, vals in caps.items():
            if len(vals) > 1:
                add("warn", cid, f"cap bucket '{b}' is shared by rules with different caps {sorted(vals)}: the largest applies")
    return issues


def _rkey(r: dict) -> tuple:
    return (r["id"], r.get("effective_from"), r.get("effective_to"))


def _rlabel(r: dict) -> str:
    w = ""
    if r.get("effective_from") or r.get("effective_to"):
        w = f" [{r.get('effective_from') or 'always'} to {r.get('effective_to') or 'open'}]"
    return f"rule '{r['id']}'{w}"


def _rules_by_key(card: dict) -> dict:
    """Dated versions of one rule id are separate entries, so a scheduled change is not collapsed into the live one."""
    return {_rkey(r): r for r in card.get("rules", [])}


def _live(card: dict, day: str) -> dict:
    """rule id -> rule in force on `day` (ISO)."""
    return {r["id"]: r for r in card.get("rules", [])
            if (r.get("effective_from") or "") <= day and (not r.get("effective_to") or day < r["effective_to"])}


def diff(old: dict | None, new: dict) -> dict:
    old_cards = {c["id"]: c for c in (old or {"cards": []})["cards"]}
    new_cards = {c["id"]: c for c in new["cards"]}
    added = sorted(new_cards.keys() - old_cards.keys())
    removed = sorted(old_cards.keys() - new_cards.keys())
    changed: dict[str, list[str]] = {}
    review: list[str] = []
    for cid in removed:
        review.append(f"card '{cid}' removed: users holding it lose recommendations")
    for cid in sorted(old_cards.keys() & new_cards.keys()):
        o, n = old_cards[cid], new_cards[cid]
        ch: list[str] = []
        if o["base_rate_bps"] != n["base_rate_bps"]:
            ch.append(f"base rate {o['base_rate_bps'] / 100:.2f}% -> {n['base_rate_bps'] / 100:.2f}%")
            if abs(o["base_rate_bps"] - n["base_rate_bps"]) >= LARGE_RATE_CHANGE_BPS:
                review.append(f"{cid}: base rate moved by {abs(o['base_rate_bps'] - n['base_rate_bps']) / 100:.1f} points")
        orr, nrr = _rules_by_key(o), _rules_by_key(n)
        for k in sorted(nrr.keys() - orr.keys(), key=str):
            ch.append(f"{_rlabel(nrr[k])} added ({nrr[k]['rate_bps'] / 100:.2f}%)")
        for k in sorted(orr.keys() - nrr.keys(), key=str):
            ch.append(f"{_rlabel(orr[k])} removed (was {orr[k]['rate_bps'] / 100:.2f}%)")
        today = date.today().isoformat()
        lo, ln = _live(o, today), _live(n, today)
        for rid in sorted(lo.keys() - ln.keys()):
            review.append(f"{cid}: rule '{rid}' ({lo[rid]['rate_bps'] / 100:.2f}%) is live today and would stop applying")
        # a scheduled change: a dated version that is not live yet, against the version live today
        for k, r in sorted(nrr.items(), key=lambda kv: str(kv[0])):
            start = r.get("effective_from")
            if start and start > today and k not in orr and k[0] in lo:
                delta = abs(r["rate_bps"] - lo[k[0]]["rate_bps"])
                if delta >= LARGE_RATE_CHANGE_BPS:
                    review.append(f"{cid}: rule '{k[0]}' scheduled to move {lo[k[0]]['rate_bps'] / 100:.2f}% -> {r['rate_bps'] / 100:.2f}% on {start} ({delta / 100:.1f} points)")
        for k in sorted(orr.keys() & nrr.keys(), key=str):
            rid = k[0]
            a, b = orr[k], nrr[k]
            lab = _rlabel(b)
            if a["rate_bps"] != b["rate_bps"]:
                ch.append(f"{lab} rate {a['rate_bps'] / 100:.2f}% -> {b['rate_bps'] / 100:.2f}%")
                if abs(a["rate_bps"] - b["rate_bps"]) >= LARGE_RATE_CHANGE_BPS:
                    review.append(f"{cid}: {lab} rate moved by {abs(a['rate_bps'] - b['rate_bps']) / 100:.1f} points")
            if a.get("cap_paise") != b.get("cap_paise"):
                ch.append(f"{lab} cap {a.get('cap_paise')} -> {b.get('cap_paise')} paise")
            for k in ("merchants", "categories", "mccs", "channels"):
                if sorted(a.get(k, [])) != sorted(b.get(k, [])):
                    ch.append(f"{lab} {k} changed")
        for k in ("excluded_categories", "excluded_mccs", "supported_channels"):
            if sorted(o.get(k, [])) != sorted(n.get(k, [])):
                gone = sorted(set(o.get(k, [])) - set(n.get(k, [])))
                came = sorted(set(n.get(k, [])) - set(o.get(k, [])))
                ch.append(f"{k}: +{came} -{gone}")
        if o.get("surcharge_bps", {}) != n.get("surcharge_bps", {}) or o.get("surcharge_waiver", {}) != n.get("surcharge_waiver", {}):
            ch.append("surcharges changed")
        if ch:
            changed[cid] = ch
    return {"added": added, "removed": removed, "changed": changed, "review_required": review,
            "summary": f"{len(added)} added, {len(removed)} removed, {len(changed)} changed"}


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rules-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # readers see the old file or the new one, never half of either
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _log(releases: Path, entry: dict) -> None:
    releases.mkdir(parents=True, exist_ok=True)
    with open(releases / "history.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def history(releases: Path = RELEASES) -> list[dict]:
    p = releases / "history.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def publish(new: dict, live: Path = LIVE, releases: Path = RELEASES, approve_large: bool = False,
            dry_run: bool = False, actor: str = "cli", today: date | None = None) -> dict:
    issues = lint(new, today)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise PublishRefused("rules rejected: " + "; ".join(f"{i['card'] or 'file'}: {i['msg']}" for i in errors), 2)
    old = json.loads(live.read_text()) if live.exists() else None
    d = diff(old, new)
    version, previous = rules_version(new), (rules_version(old) if old else None)
    report = {"version": version, "previous": previous, "diff": d, "issues": issues, "published": False, "dry_run": dry_run}
    if version == previous:
        report["note"] = "identical to what is live; nothing to publish"
        return report
    if d["review_required"] and not approve_large:
        raise PublishRefused("needs approval (re-run with --approve-large after review): " + "; ".join(d["review_required"]), 3)
    if dry_run:
        return report
    if old is not None:
        _atomic_write(releases / f"{previous}.json", old)
    _atomic_write(releases / f"{version}.json", new)
    _atomic_write(live, new)
    _log(releases, {"action": "publish", "version": version, "previous": previous, "actor": actor,
                    "at": datetime.now(timezone.utc).isoformat(), "summary": d["summary"],
                    "approved_large": bool(d["review_required"])})
    report["published"] = True
    return report


def rollback(live: Path = LIVE, releases: Path = RELEASES, version: str | None = None, actor: str = "cli") -> dict:
    current = rules_version(json.loads(live.read_text())) if live.exists() else None
    if version is None:
        pubs = [h for h in history(releases) if h.get("previous")]
        # the most recent release whose previous differs from the live one is the one to go back to
        prev = next((h["previous"] for h in reversed(pubs) if h["version"] == current), None)
        if not prev:
            raise PublishRefused("no earlier release recorded to roll back to", 1)
        version = prev
    target = releases / f"{version}.json"
    if not target.exists():
        raise PublishRefused(f"release {version} not found in {releases}", 1)
    data = json.loads(target.read_text())
    try:
        validate_catalog(data)
    except CatalogError as e:
        raise PublishRefused(f"release {version} no longer validates: {e}", 2) from None
    if rules_version(data) != version:
        raise PublishRefused(f"release file {version}.json does not match its hash (tampered or corrupt)", 2)
    _atomic_write(live, data)
    _log(releases, {"action": "rollback", "version": version, "previous": current, "actor": actor,
                    "at": datetime.now(timezone.utc).isoformat(), "summary": f"rolled back {current} -> {version}"})
    return {"version": version, "previous": current, "rolled_back": True}


def _load(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise PublishRefused(f"cannot read {path}: {e}", 1) from None


def _print_issues(issues: list[dict]) -> None:
    for i in issues:
        print(f"  [{i['level']:5}] {i['card'] or 'file':16} {i['msg']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scruge.rules", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("dir"); b.add_argument("-o", "--out", required=True); b.add_argument("--note", default="")
    l = sub.add_parser("lint"); l.add_argument("file")
    d = sub.add_parser("diff"); d.add_argument("old"); d.add_argument("new")
    p = sub.add_parser("publish"); p.add_argument("file"); p.add_argument("--approve-large", action="store_true")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--live", default=str(LIVE)); p.add_argument("--releases", default=str(RELEASES))
    p.add_argument("--actor", default=os.environ.get("USER", "cli"))
    r = sub.add_parser("rollback"); r.add_argument("version", nargs="?"); r.add_argument("--live", default=str(LIVE))
    r.add_argument("--releases", default=str(RELEASES)); r.add_argument("--actor", default=os.environ.get("USER", "cli"))
    h = sub.add_parser("history"); h.add_argument("--releases", default=str(RELEASES))
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            data = build_from_dir(a.dir, a.note)
            _atomic_write(Path(a.out), data)
            print(f"built {len(data['cards'])} cards -> {a.out} (version {rules_version(data)})")
        elif a.cmd == "lint":
            issues = lint(_load(a.file)); _print_issues(issues)
            print(f"{sum(i['level'] == 'error' for i in issues)} errors, {sum(i['level'] == 'warn' for i in issues)} warnings")
            return 2 if any(i["level"] == "error" for i in issues) else 0
        elif a.cmd == "diff":
            dd = diff(_load(a.old), _load(a.new)); print(dd["summary"])
            for k in ("added", "removed"):
                for cid in dd[k]:
                    print(f"  {k}: {cid}")
            for cid, ch in dd["changed"].items():
                print(f"  changed: {cid}"); [print(f"    - {c}") for c in ch]
            for rr in dd["review_required"]:
                print(f"  REVIEW: {rr}")
        elif a.cmd == "publish":
            rep = publish(_load(a.file), Path(a.live), Path(a.releases), a.approve_large, a.dry_run, a.actor)
            _print_issues(rep["issues"]); print(rep["diff"]["summary"])
            print(rep.get("note") or (f"{'would publish' if a.dry_run else 'published'} {rep['version']} (previous {rep['previous']})"))
        elif a.cmd == "rollback":
            rep = rollback(Path(a.live), Path(a.releases), a.version, a.actor)
            print(f"rolled back {rep['previous']} -> {rep['version']}")
        elif a.cmd == "history":
            for e in history(Path(a.releases)):
                print(f"{e['at'][:19]}  {e['action']:8} {e['version']}  (was {e.get('previous')})  {e.get('actor', '')}  {e.get('summary', '')}")
    except PublishRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
