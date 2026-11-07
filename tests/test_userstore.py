"""Server-side wallet, cap usage and ledger: atomic, idempotent, cycle-scoped."""
import threading

import pytest

from scruge.userstore import UserStore, current_cycle
from tests.conftest import body

UID = "user-store-01"
SWIGGY = dict(amount=3000, mcc="5814", channel="online", merchant="swiggy", wallet=["hdfc_swiggy", "hdfc_infinia"])


def pay(c, ref, **kw):
    return c.post("/api/commit", json={**body(**{**SWIGGY, **kw}), "user_id": UID, "txn_ref": ref})


def test_state_survives_between_requests_and_drives_the_next_answer(make_client):
    c = make_client()
    assert c.get(f"/api/users/{UID}/state").json()["exists"] is False
    r = pay(c, "ref-00001").json()
    assert r["persisted"] and r["cycle"] == current_cycle() and r["used"]["card_id"] == "hdfc_swiggy"
    for i in range(2, 6):
        pay(c, f"ref-0000{i}")
    st = c.get(f"/api/users/{UID}/state").json()
    assert st["exists"] and st["totals"]["count"] == 5 and st["cycle_usage_paise"]["hdfc_swiggy"]["swiggy"] == 150_000   # capped at 1,500
    nxt = pay(c, "ref-00006").json()
    assert nxt["used"]["card_id"] == "hdfc_infinia"            # the server remembered the exhausted cap, the client sent nothing


def test_retry_with_same_txn_ref_applies_nothing_twice(make_client):
    c = make_client()
    a = pay(c, "retry-0001")
    b = pay(c, "retry-0001")
    assert b.headers.get("Idempotent-Replay") == "true" and a.json() == b.json()
    st = c.get(f"/api/users/{UID}/state").json()
    assert st["totals"]["count"] == 1 and st["cycle_usage_paise"]["hdfc_swiggy"]["swiggy"] == 30_000


def test_client_cannot_rewind_its_own_caps(make_client):
    c = make_client()
    for i in range(5):
        pay(c, f"burn-000{i}")
    r = c.post("/api/commit", json={**body(**SWIGGY), "user_id": UID, "txn_ref": "rewind-01", "cycle_usage_paise": {"hdfc_swiggy": {"swiggy": 0}}}).json()
    assert r["used"]["card_id"] == "hdfc_infinia"             # stored usage wins over what the client claims


def test_wallet_roundtrip_reset_and_validation(make_client):
    c = make_client()
    assert c.put(f"/api/users/{UID}/wallet", json={"cards": ["sbi_cashback", "hdfc_swiggy", "sbi_cashback"]}).json()["wallet"] == ["sbi_cashback", "hdfc_swiggy"]
    assert c.get(f"/api/users/{UID}/state").json()["wallet"] == ["sbi_cashback", "hdfc_swiggy"]
    assert c.put(f"/api/users/{UID}/wallet", json={"cards": ["nope"]}).status_code == 422
    pay(c, "reset-0001")
    assert c.post(f"/api/users/{UID}/reset").json()["reset"]
    st = c.get(f"/api/users/{UID}/state").json()
    assert st["totals"]["count"] == 0 and st["cycle_usage_paise"] == {} and st["wallet"] == ["sbi_cashback", "hdfc_swiggy"]
    assert c.get("/api/users/short/state").status_code == 422
    assert c.get(f"/api/users/{UID}/state?cycle=2026-13-xx").status_code == 422


def test_cycles_are_isolated(make_client):
    c = make_client()
    c.post("/api/commit", json={**body(**SWIGGY), "user_id": UID, "txn_ref": "cyc-a-001", "cycle": "2026-09"})
    assert c.get(f"/api/users/{UID}/state?cycle=2026-09").json()["totals"]["count"] == 1
    assert c.get(f"/api/users/{UID}/state?cycle=2026-10").json()["totals"]["count"] == 0


def test_failed_commit_leaves_no_partial_state(make_client):
    c = make_client()
    r = c.post("/api/commit", json={**body(**SWIGGY), "user_id": UID, "txn_ref": "bad-card-01", "card_id": "sbi_cashback"})
    assert r.status_code == 422                                # not in wallet
    assert c.get(f"/api/users/{UID}/state").json()["totals"]["count"] == 0


def test_concurrent_commits_lose_no_updates():
    s = UserStore(":memory:")
    n = 40

    def work(i):
        def compute(usage):
            cur = usage.get("c", {}).get("b", 0)
            return {"c": {"b": cur + 1}}, {"txn_ref": f"t{i:04d}", "created_at": "x", "amount_paise": 1, "card_id": "c",
                                           "expected_paise": 1, "surcharge_paise": 0, "net_paise": 1}, {"i": i}
        s.commit("u1234567", "2026-10", f"t{i:04d}", compute)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert s.get_usage("u1234567", "2026-10") == {"c": {"b": n}} and len(s.get_ledger("u1234567")) == n


def test_concurrent_identical_txn_ref_applies_once():
    s = UserStore(":memory:")
    calls = []

    def compute(usage):
        calls.append(1)
        return {"c": {"b": usage.get("c", {}).get("b", 0) + 5}}, {"txn_ref": "same-ref", "created_at": "x", "amount_paise": 1, "card_id": "c",
                                                                  "expected_paise": 1, "surcharge_paise": 0, "net_paise": 1}, {"ok": 1}
    res = []
    ts = [threading.Thread(target=lambda: res.append(s.commit("u1234567", "2026-10", "same-ref", compute))) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(calls) == 1 and s.get_usage("u1234567", "2026-10") == {"c": {"b": 5}} and sum(1 for _, rep in res if rep) == 11


def test_usage_check_constraint_rejects_negative():
    s = UserStore(":memory:")
    with pytest.raises(Exception):
        s.commit("u1234567", "2026-10", "neg-0001", lambda u: ({"c": {"b": -1}}, {"txn_ref": "neg-0001", "created_at": "x", "amount_paise": 1, "card_id": "c",
                                                                                 "expected_paise": 1, "surcharge_paise": 0, "net_paise": 1}, {}))
    assert s.get_usage("u1234567", "2026-10") == {} and s.get_ledger("u1234567") == []


def test_replay_with_a_different_body_is_a_409_like_the_stateless_path(make_client):
    c = make_client()
    a = pay(c, "conflict-001")
    assert a.status_code == 200
    same = pay(c, "conflict-001")                                                   # identical body: a harmless retry
    assert same.status_code == 200 and same.headers.get("Idempotent-Replay") == "true" and same.json() == a.json()
    other = pay(c, "conflict-001", amount=1)                                        # same reference, Rs 1 instead of Rs 3,000
    assert other.status_code == 409 and "different request body" in other.text
    other_card = c.post("/api/commit", json={**body(**SWIGGY), "user_id": UID, "txn_ref": "conflict-001", "card_id": "hdfc_infinia"})
    assert other_card.status_code == 409
    st = c.get(f"/api/users/{UID}/state").json()
    assert st["totals"]["count"] == 1 and st["cycle_usage_paise"]["hdfc_swiggy"]["swiggy"] == 30_000      # nothing applied twice


def test_old_databases_without_fingerprints_are_migrated_and_still_replay(tmp_path):
    import sqlite3

    from scruge.userstore import SCHEMA
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA.replace(", fingerprint TEXT,", ","))          # the schema as it was before this column existed
    db.execute("INSERT INTO users VALUES ('olduser-0001','x')")
    db.execute("INSERT INTO ledger (user_id,cycle,txn_ref,created_at,amount_paise,card_id,expected_paise,surcharge_paise,net_paise,response) "
               "VALUES ('olduser-0001','2026-10','old-ref-0001','x',1,'c',1,0,1,'{\"old\":true}')")
    db.commit(); db.close()
    s = UserStore(path)                                                    # opens and migrates
    resp, replayed = s.commit("olduser-0001", "2026-10", "old-ref-0001", lambda u: (u, {}, {}), "any-fingerprint")
    assert replayed and resp == {"old": True}                              # no fingerprint stored: cannot compare, so it replays
