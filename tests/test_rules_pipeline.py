"""Rules pipeline: build, lint, diff, publish (with a review gate) and rollback."""
import copy
import json
from datetime import date

import pytest

from rewards_engine import rules
from rewards_engine.catalog import rules_version
from tests.conftest import DATA

LIVE = json.loads(DATA.read_text())


def card(cid="c1", **kw):
    c = {"id": cid, "name": f"Card {cid}", "network": "visa", "base_rate_bps": 100, "rules": [
        {"id": "r1", "rate_bps": 500, "merchants": ["amazon"]}], "source": "https://example.com", "confidence": "illustrative"}
    c.update(kw)
    return c


def levels(issues, level):
    return [i["msg"] for i in issues if i["level"] == level]


# ---- build ----
def test_build_merges_one_file_per_card_sorted(tmp_path):
    for cid in ("b", "a"):
        (tmp_path / f"{cid}.json").write_text(json.dumps(card(cid)))
    assert [c["id"] for c in rules.build_from_dir(tmp_path)["cards"]] == ["a", "b"]


def test_build_honours_display_order_then_id(tmp_path):
    for cid, order in (("a", 30), ("b", 10), ("c", 10)):
        (tmp_path / f"{cid}.json").write_text(json.dumps(card(cid, display_order=order)))
    assert [c["id"] for c in rules.build_from_dir(tmp_path)["cards"]] == ["b", "c", "a"]


def test_build_rejects_mismatched_names_bad_json_and_empty(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps(card("y")))
    with pytest.raises(rules.PublishRefused, match="must match the card id"):
        rules.build_from_dir(tmp_path)
    (tmp_path / "x.json").write_text("{nope")
    with pytest.raises(rules.PublishRefused, match="invalid JSON"):
        rules.build_from_dir(tmp_path)
    with pytest.raises(rules.PublishRefused):
        rules.build_from_dir(tmp_path / "missing")


def test_per_card_files_rebuild_exactly_the_live_rules():
    """data/cards.d/*.json is the editable source; data/cards.json is what is served. They must not drift."""
    built = rules.build_from_dir(DATA.parent / "cards.d", LIVE.get("_note", ""))
    assert rules_version(built) == rules_version(LIVE)


# ---- lint ----
def test_lint_current_data_has_no_errors_or_warnings():
    issues = rules.lint(LIVE)
    assert not levels(issues, "error") and not levels(issues, "warn")
    assert not any("below the base rate" in m for m in levels(issues, "info")), "a rule below the base rate can never pay: fix the data"


def test_lint_errors_and_warnings():
    d = {"cards": [card("a", rules=[{"id": "r", "rate_bps": 4000}], source=None, name="Dup"),
                   card("b", name="dup", confidence="verified", verified_on=None, base_rate_bps=1500)]}
    issues = rules.lint(d, date(2026, 11, 7))
    assert any("requires verified_on" in m for m in levels(issues, "error"))
    warns = levels(issues, "warn")
    assert any("no source" in m for m in warns) and any("points vs rupees" in m for m in warns)
    assert any("same name" in m for m in warns) and any("looks high" in m for m in warns)


def test_lint_stale_and_future_verification():
    d = {"cards": [card("a", confidence="verified", verified_on="2026-01-01"), card("b", confidence="reviewed", verified_on="2027-01-01")]}
    issues = rules.lint(d, date(2026, 11, 7))
    assert any("days ago" in m for m in levels(issues, "warn")) and any("in the future" in m for m in levels(issues, "error"))


def test_lint_reports_structure_errors_without_crashing():
    assert levels(rules.lint({"cards": []}), "error") and levels(rules.lint({"cards": [{"id": "x"}]}), "error")


def test_lint_shared_bucket_with_different_caps_warns():
    c = card("a", rules=[{"id": "r1", "rate_bps": 500, "cap_paise": 100, "cap_bucket": "b"}, {"id": "r2", "rate_bps": 300, "cap_paise": 200, "cap_bucket": "b"}])
    assert any("shared by rules with different caps" in m for m in levels(rules.lint({"cards": [c]}), "warn"))


# ---- diff ----
def test_diff_describes_changes_and_flags_large_ones():
    new = copy.deepcopy(LIVE)
    sw = next(c for c in new["cards"] if c["id"] == "hdfc_swiggy")
    sw["rules"][0]["rate_bps"] = 1500                       # +5 points: needs a human
    sw["rules"][1]["cap_paise"] = 300_000
    new["cards"] = [c for c in new["cards"] if c["id"] != "sbi_cashback"]
    new["cards"].append(card("new_card"))
    d = rules.diff(LIVE, new)
    assert d["added"] == ["new_card"] and d["removed"] == ["sbi_cashback"] and set(d["changed"]) == {"hdfc_swiggy"}
    assert any("10.00% -> 15.00%" in c for c in d["changed"]["hdfc_swiggy"])
    assert any("moved by 5.0 points" in r for r in d["review_required"]) and any("removed" in r for r in d["review_required"])


def test_diff_of_identical_rules_is_empty():
    d = rules.diff(LIVE, copy.deepcopy(LIVE))
    assert d["summary"] == "0 added, 0 removed, 0 changed" and not d["review_required"]


# ---- publish / rollback ----
@pytest.fixture
def env(tmp_path):
    live = tmp_path / "cards.json"
    live.write_text(json.dumps(LIVE))
    return live, tmp_path / "releases"


def small_change(rate=600):
    n = copy.deepcopy(LIVE)
    next(c for c in n["cards"] if c["id"] == "sbi_cashback")["rules"][0]["rate_bps"] = rate
    return n


def test_publish_archives_swaps_and_logs(env):
    live, rel = env
    new = small_change(600)
    rep = rules.publish(new, live, rel, actor="tester")
    assert rep["published"] and rep["previous"] == rules_version(LIVE) and rep["version"] == rules_version(new)
    assert json.loads(live.read_text()) == new
    assert (rel / f"{rules_version(LIVE)}.json").exists() and (rel / f"{rules_version(new)}.json").exists()
    h = rules.history(rel)
    assert h[-1]["action"] == "publish" and h[-1]["actor"] == "tester" and h[-1]["summary"] == "0 added, 0 removed, 1 changed"


def test_publish_dry_run_and_identical_change_nothing(env):
    live, rel = env
    before = live.read_text()
    rep = rules.publish(small_change(600), live, rel, dry_run=True)
    assert not rep["published"] and live.read_text() == before and not rel.exists()
    same = rules.publish(copy.deepcopy(LIVE), live, rel)
    assert not same["published"] and "identical" in same["note"]


def test_publish_refuses_invalid_rules_and_leaves_live_untouched(env):
    live, rel = env
    before = live.read_text()
    bad = copy.deepcopy(LIVE); bad["cards"][0]["base_rate_bps"] = -5
    with pytest.raises(rules.PublishRefused) as e:
        rules.publish(bad, live, rel)
    assert e.value.code == 2 and live.read_text() == before


def test_large_changes_need_explicit_approval(env):
    live, rel = env
    with pytest.raises(rules.PublishRefused) as e:
        rules.publish(small_change(1500), live, rel)           # 5% -> 15%
    assert e.value.code == 3 and "needs approval" in str(e.value)
    assert rules.publish(small_change(1500), live, rel, approve_large=True)["published"]
    assert rules.history(rel)[-1]["approved_large"] is True


def test_rollback_restores_previous_release_and_verifies_its_hash(env):
    live, rel = env
    rules.publish(small_change(600), live, rel)
    rules.publish(small_change(700), live, rel)
    r = rules.rollback(live, rel)
    assert r["version"] == rules_version(small_change(600)) and json.loads(live.read_text()) == small_change(600)
    assert rules.rollback(live, rel, version=rules_version(LIVE))["rolled_back"] and json.loads(live.read_text()) == LIVE
    assert [h["action"] for h in rules.history(rel)][-2:] == ["rollback", "rollback"]


def test_rollback_refuses_unknown_and_tampered_releases(env):
    live, rel = env
    rules.publish(small_change(600), live, rel)
    with pytest.raises(rules.PublishRefused, match="not found"):
        rules.rollback(live, rel, version="deadbeefdeadbeef")
    old = rel / f"{rules_version(LIVE)}.json"
    t = json.loads(old.read_text()); t["cards"][0]["base_rate_bps"] = 999
    old.write_text(json.dumps(t))                               # content no longer matches its name
    with pytest.raises(rules.PublishRefused, match="does not match its hash"):
        rules.rollback(live, rel, version=rules_version(LIVE))


def test_rollback_with_no_history_says_so(env):
    live, rel = env
    with pytest.raises(rules.PublishRefused, match="no earlier release"):
        rules.rollback(live, rel)


def test_service_picks_up_a_published_release_and_a_rollback(make_client, rules_file, tmp_path):
    """The whole loop: publish -> the running service serves new rules -> rollback -> it serves the old ones again."""
    c = make_client(admin_token="t")
    v0 = c.get("/api/meta").json()["rules_version"]
    rel = tmp_path / "rel"
    new = small_change(900)
    rules.publish(new, rules_file, rel)
    assert c.post("/api/admin/reload", headers={"X-Admin-Token": "t"}).json()["status"] == "reloaded"
    v1 = c.get("/api/meta").json()["rules_version"]
    assert v1 == rules_version(new) != v0
    rules.rollback(rules_file, rel)
    c.post("/api/admin/reload", headers={"X-Admin-Token": "t"})
    assert c.get("/api/meta").json()["rules_version"] == v0


def test_cli_exit_codes(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps(LIVE))
    assert rules.main(["lint", str(f)]) == 0
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps({"cards": []}))
    assert rules.main(["lint", str(bad)]) == 2
    assert rules.main(["lint", str(tmp_path / "missing.json")]) == 1
    big = tmp_path / "big.json"; big.write_text(json.dumps(small_change(1500)))
    assert rules.main(["publish", str(big), "--live", str(f), "--releases", str(tmp_path / "r")]) == 3
    assert rules.main(["publish", str(big), "--live", str(f), "--releases", str(tmp_path / "r"), "--approve-large"]) == 0
    assert rules.main(["history", "--releases", str(tmp_path / "r")]) == 0
    assert rules.main(["rollback", "--live", str(f), "--releases", str(tmp_path / "r")]) == 0
