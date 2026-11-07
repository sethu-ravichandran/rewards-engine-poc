"""Bad input is a 422, never a 500; unmatched credit refs are reported; a card listed twice is one card."""
import threading

from tests.conftest import body

H = {"content-type": "application/json"}


def test_nan_and_infinity_are_422_not_500(make_client):
    c = make_client()
    for bad in ("NaN", "Infinity", "-Infinity"):
        raw = '{"transaction":{"amount":%s,"mcc":"5411","channel":"card"},"wallet":["sbi_cashback"]}' % bad
        assert c.post("/api/optimize", content=raw, headers=H).status_code == 422
        assert c.post("/api/commit", content=raw.replace("}}", "}, \"user_id\":\"nan_user_01\"}"), headers=H).status_code == 422


def test_cycle_must_be_a_real_month(make_client):
    c = make_client()
    for cyc in ("2026-13", "2026-00", "1999-01", "2026-1"):
        assert c.post("/api/users/cycle_user_01/reset", params={"cycle": cyc}).status_code == 422
    assert c.post("/api/users/cycle_user_01/reset", params={"cycle": "2026-11"}).status_code == 200


def test_unknown_credit_refs_are_reported(make_client):
    c = make_client()
    r = c.post("/api/users/credit_user_01/credits", json={"credits": [{"txn_ref": "nope", "actual_paise": 5}]}).json()
    assert r["recorded"] == 0 and r["unmatched_refs"] == ["nope"]


def test_duplicate_wallet_cards_count_once(make_client):
    c = make_client()
    b = body(wallet=["sbi_cashback", "sbi_cashback"])
    assert len(c.post("/api/optimize", json=b).json()["ranked"]) == 1


def test_two_devices_one_cap_never_exceed_it(make_client):
    """Many concurrent commits for one user against one capped rule: the cap is applied exactly, never exceeded."""
    c = make_client()
    out = []

    def pay(i):
        b = body(mcc="5814", channel="online", merchant="swiggy", wallet=["hdfc_swiggy"], amount=5000)
        b.update(user_id="race_user_01", txn_ref=f"race{i:05d}")
        out.append(c.post("/api/commit", json=b).json())
    ts = [threading.Thread(target=pay, args=(i,)) for i in range(16)]
    [t.start() for t in ts]; [t.join() for t in ts]
    rewards = sum(o["best"]["reward_paise"] for o in out)
    assert len(out) == 16 and len({o["best"]["card_id"] for o in out}) == 1
    # total reward equals what 16 sequential commits would have produced
    c2 = make_client()
    seq = 0
    for i in range(16):
        b = body(mcc="5814", channel="online", merchant="swiggy", wallet=["hdfc_swiggy"], amount=5000)
        b.update(user_id="seq_user_001", txn_ref=f"seq{i:05d}")
        seq += c2.post("/api/commit", json=b).json()["best"]["reward_paise"]
    assert rewards == seq


def test_stateless_deployment_has_no_user_state(make_client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    c = make_client()
    assert c.get("/api/users/stateless_user1/state").status_code == 404
    b = body(wallet=["sbi_cashback"], amount=1000)                  # the stateless engine routes still work
    assert c.post("/api/optimize", json=b).status_code == 200
