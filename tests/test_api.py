from fastapi.testclient import TestClient

from rewards_engine.api import app

client = TestClient(app)
WALLET = ["amazon_icici", "hdfc_swiggy", "hdfc_infinia"]


def body(**kw):
    b = {
        "transaction": {"amount": 1000, "mcc": "5814", "channel": "online", "merchant": "swiggy"},
        "wallet": WALLET,
    }
    b.update(kw)
    return b


def test_meta():
    r = client.get("/api/meta").json()
    assert len(r["cards"]) >= 5 and r["merchants"]


def test_optimize():
    r = client.post("/api/optimize", json=body()).json()
    assert r["best"]["card_id"] == "hdfc_swiggy"
    assert r["best"]["reward_inr"] == 100.0
    assert r["best"]["why"]


def test_commit_updates_usage_and_flags_regret():
    r = client.post("/api/commit", json=body(card_id="hdfc_infinia")).json()
    assert r["used"]["card_id"] == "hdfc_infinia"
    assert r["left_on_table_inr"] == 100.0 - 33.0
    r = client.post("/api/commit", json=body()).json()
    assert r["cycle_usage"]["hdfc_swiggy"]["swiggy"] == 100.0
    assert r["left_on_table_inr"] == 0


def test_validation():
    assert client.post("/api/optimize", json=body(wallet=["nope"])).status_code == 422
    bad = body()
    bad["transaction"]["mcc"] = "abc"
    assert client.post("/api/optimize", json=bad).status_code == 422


def test_index_served():
    assert client.get("/").status_code == 200
