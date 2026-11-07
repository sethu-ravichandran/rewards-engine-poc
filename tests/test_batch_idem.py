
TXN = {"amount": 2000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}


def batch(n=3, **kw):
    b = {"transactions": [dict(TXN) for _ in range(n)], "wallet": ["hdfc_swiggy", "hdfc_infinia"]}
    b.update(kw)
    return b


def test_batch_non_sequential_matches_single_calls(make_client):
    c = make_client()
    txns = [TXN, dict(TXN, amount=500, merchant=None, mcc="5411", channel="tap"), dict(TXN, channel="upi")]
    r = c.post("/api/optimize/batch", json=batch(transactions=txns, explain=True))
    assert r.status_code == 200
    out = r.json()
    assert out["count"] == 3 and out["sequential"] is False and "cycle_usage" not in out
    for t, res in zip(txns, out["results"]):
        single = c.post("/api/optimize", json={"transaction": t, "wallet": ["hdfc_swiggy", "hdfc_infinia"]}).json()
        assert res == single


def test_batch_default_skips_explanations(make_client):
    out = make_client().post("/api/optimize/batch", json=batch(1)).json()
    assert all(e["why"] == [] for e in out["results"][0]["ranked"])


def test_batch_sequential_simulates_a_month(make_client):
    """Sequential batch == calling /commit one by one with the best card and threading usage through."""
    c = make_client()
    n = 12
    seq = c.post("/api/optimize/batch", json=batch(n, sequential=True)).json()
    usage, bests = {}, []
    for _ in range(n):
        r = c.post("/api/commit", json={"transaction": TXN, "wallet": ["hdfc_swiggy", "hdfc_infinia"], "cycle_usage": usage}).json()
        usage, bests = r["cycle_usage"], bests + [r["best"]["card_id"]]
    assert [x["best"]["card_id"] for x in seq["results"]] == bests
    assert seq["cycle_usage"] == usage
    assert seq["cycle_usage"]["hdfc_swiggy"]["swiggy"] == 1500.0           # cap exhausted...
    assert bests[0] == "hdfc_swiggy" and bests[-1] == "hdfc_infinia"       # ...so the winner switched mid-month
    assert seq["totals"]["reward_inr"] == sum(x["best"]["reward_inr"] for x in seq["results"])
    # without sequential, every txn sees a fresh cap and picks swiggy
    flat = c.post("/api/optimize/batch", json=batch(n)).json()
    assert {x["best"]["card_id"] for x in flat["results"]} == {"hdfc_swiggy"}


def test_batch_starting_usage(make_client):
    out = make_client().post("/api/optimize/batch", json=batch(1, sequential=True, cycle_usage={"hdfc_swiggy": {"swiggy": 1500}})).json()
    assert out["results"][0]["best"]["card_id"] == "hdfc_infinia"


def test_batch_limits(make_client):
    c = make_client(max_batch=5)
    assert c.post("/api/optimize/batch", json=batch(5)).status_code == 200
    r = c.post("/api/optimize/batch", json=batch(6))
    assert r.status_code == 422 and "too many transactions" in r.json()["detail"]
    assert c.post("/api/optimize/batch", json=batch(0)).status_code == 422
    assert c.post("/api/optimize/batch", json=batch(2, wallet=["nope"])).status_code == 422
    assert make_client(max_amount_inr=100).post("/api/optimize/batch", json=batch(1)).status_code == 422


def test_batch_200_default_and_threadpool_path(make_client):
    c = make_client()
    r = c.post("/api/optimize/batch", json=batch(200, sequential=True))
    assert r.status_code == 200 and r.json()["count"] == 200
    assert c.post("/api/optimize/batch", json=batch(201)).status_code == 422


def test_batch_gzip_when_large(make_client):
    c = make_client()
    r = c.post("/api/optimize/batch", json=batch(60), headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.json()["count"] == 60
    r = c.post("/api/optimize/batch", json=batch(60), headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
