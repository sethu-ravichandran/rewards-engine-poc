import gzip
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rewards_engine.api import OptimizeIn, create_app
from rewards_engine.config import Settings
from rewards_engine.obs import JsonFormatter
from tests.conftest import body


def test_meta_etag_304_cache_control_and_gzip(make_client):
    c = make_client()
    r = c.get("/api/meta", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    etag = r.headers["etag"]
    assert etag.startswith('"') and "max-age=30" in r.headers["cache-control"]
    assert r.json()["rules_version"] == r.headers["x-rules-version"]
    # conditional request
    r304 = c.get("/api/meta", headers={"If-None-Match": etag})
    assert r304.status_code == 304 and r304.content == b"" and r304.headers["etag"] == etag
    assert c.get("/api/meta", headers={"If-None-Match": f'W/{etag}, "other"'}).status_code == 304
    assert c.get("/api/meta", headers={"If-None-Match": '"nope"'}).status_code == 200
    # gzip: raw bytes are compressed only when asked, and decode to the same payload
    raw = c.get("/api/meta", headers={"Accept-Encoding": "gzip"})  # httpx transparently decodes
    assert raw.headers["content-encoding"] == "gzip" and raw.json() == r.json()
    assert "content-encoding" not in c.get("/api/meta", headers={"Accept-Encoding": "gzip;q=0"}).headers
    snap = c.app.state.catalog.get()
    assert gzip.decompress(snap.meta_gzip) == snap.meta_body and len(snap.meta_gzip) < len(snap.meta_body)


def test_meta_gzip_threshold(make_client):
    c = make_client(gzip_min_bytes=10_000_000)
    assert "content-encoding" not in c.get("/api/meta", headers={"Accept-Encoding": "gzip"}).headers


def test_meta_etag_changes_with_rules(make_client, rules_file):
    from tests.conftest import write_rules
    c = make_client(admin_token="t")
    e1 = c.get("/api/meta").headers["etag"]
    write_rules(rules_file, lambda d: d["cards"][0].update(name="Renamed"))
    c.post("/api/admin/reload", headers={"X-Admin-Token": "t"})
    r = c.get("/api/meta", headers={"If-None-Match": e1})
    assert r.status_code == 200 and r.headers["etag"] != e1


def test_headers_request_id_server_timing_rules_version(make_client):
    c = make_client()
    r = c.post("/api/optimize", json=body())
    assert len(r.headers["x-request-id"]) >= 8 and r.headers["server-timing"].startswith("app;dur=")
    assert r.headers["x-rules-version"] == c.get("/api/meta").json()["rules_version"]
    assert c.get("/api/meta", headers={"X-Request-Id": "abc-123"}).headers["x-request-id"] == "abc-123"
    evil = c.get("/api/meta", headers={"X-Request-Id": "bad id\twith spaces"}).headers["x-request-id"]
    assert evil != "bad id\twith spaces" and " " not in evil


def test_health_ready(make_client):
    c = make_client()
    assert c.get("/healthz").json() == {"status": "ok"}
    r = c.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["cards"] == 5


def test_there_is_no_metrics_endpoint_or_result_cache(make_client):
    """Removed on purpose: nothing at this size needed them."""
    c = make_client()
    assert c.get("/metrics").status_code == 404
    assert not hasattr(c.app.state, "result_cache") and not hasattr(c.app.state, "idem_store")


def test_unhandled_error_is_generic_500_without_stack_trace(make_client, monkeypatch):
    c = make_client()
    import rewards_engine.api as api

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /etc/passwd")
    monkeypatch.setattr(api, "optimize", boom)
    client = TestClient(c.app, raise_server_exceptions=False)
    records = []
    h = logging.Handler()
    h.emit = records.append
    logging.getLogger("rewards_engine").addHandler(h)
    try:
        r = client.post("/api/optimize", json=body())
    finally:
        logging.getLogger("rewards_engine").removeHandler(h)
    assert r.status_code == 500
    assert r.json()["detail"] == "internal server error" and r.json()["request_id"] == r.headers["x-request-id"]
    assert "secret" not in r.text and "Traceback" not in r.text
    # the server log keeps the details, tagged with the same request id
    assert any(rec.exc_info and "secret internal detail" in str(rec.exc_info[1]) for rec in records)


def test_json_log_formatter_includes_request_id():
    from rewards_engine.obs import request_id_var
    tok = request_id_var.set("rid-1")
    try:
        rec = logging.LogRecord("rewards_engine.x", logging.WARNING, "f", 1, "hello %s", ("w",), None)
        out = json.loads(JsonFormatter().format(rec))
    finally:
        request_id_var.reset(tok)
    assert out["msg"] == "hello w" and out["request_id"] == "rid-1" and out["level"] == "WARNING"


def test_access_log_is_json_with_request_id(rules_file):
    import io
    app = create_app(Settings(rules_watch_interval_s=0, access_log=True), rules_path=rules_file)
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    logging.getLogger("rewards_engine.access").addHandler(h)
    try:
        r = TestClient(app).get("/healthz")
    finally:
        logging.getLogger("rewards_engine.access").removeHandler(h)
    d = json.loads(buf.getvalue().splitlines()[-1])
    assert d["request_id"] == r.headers["x-request-id"] and d["path"] == "/healthz" and d["status"] == 200


def test_body_too_large_413(make_client):
    c = make_client(max_body_bytes=200)
    big = body(wallet=["amazon_icici"] * 10)
    assert c.post("/api/optimize", json=big).status_code == 413
    assert c.post("/api/optimize", json=body(wallet=["amazon_icici"])).status_code == 200


def test_optimize_explain_flag(make_client):
    c = make_client()
    full = c.post("/api/optimize", json=body()).json()
    fast = c.post("/api/optimize?explain=false", json=body()).json()
    assert full["best"]["why"] and all(e["why"] == [] for e in fast["ranked"])
    strip = lambda r: {**r, "best": {**r["best"], "why": None}, "ranked": [{**e, "why": None} for e in r["ranked"]]}
    assert strip(full) == strip(fast)


def test_optimize_response_contract_unchanged(make_client):
    r = make_client().post("/api/optimize", json=body()).json()
    assert set(r) == {"best", "ranked"}
    legacy = {"card_id", "card", "eligible", "reward_inr", "surcharge_inr", "net_inr",
              "effective_rate_pct", "rule", "uses_cap", "why"}
    assert legacy <= set(r["best"])                      # nothing the old clients read was removed or renamed
    assert {"reward_paise", "surcharge_paise", "net_paise", "offer", "offer_paise", "total_paise"} <= set(r["best"])
    assert r["best"]["why"][0].startswith("MCC 5814")
    assert make_client().post("/api/optimize", json=body(wallet=["amazon_icici"], mcc="5814", channel="upi")).json()["best"] is None


def test_422_shape_matches_fastapi_default(make_client):
    """The hand-parsed hot endpoint must return the same 422 body as a stock FastAPI route."""
    ref = FastAPI()

    @ref.post("/x")
    def x(b: OptimizeIn):
        return {}
    rc = TestClient(ref)
    c = make_client()
    bad = [body(mcc="abc"), {"wallet": ["amazon_icici"]}, {**body(), "wallet": []}, body(amount=-1),
           body(channel="telepathy"), {"transaction": 5, "wallet": 5}]
    for b in bad:
        a, e = c.post("/api/optimize", json=b), rc.post("/x", json=b)
        assert a.status_code == e.status_code == 422
        assert a.json() == e.json(), b
    for raw in (b"{nope", b"", b"[1,2", b"null", b'"str"'):
        a = c.post("/api/optimize", content=raw, headers={"content-type": "application/json"})
        e = rc.post("/x", content=raw, headers={"content-type": "application/json"})
        assert a.status_code == e.status_code == 422 and a.json() == e.json(), raw


def test_limits_give_clear_422(make_client):
    c = make_client(max_wallet=2, max_amount_inr=1000, max_usage_cards=1, max_usage_buckets=1)
    r = c.post("/api/optimize", json=body(wallet=["amazon_icici", "hdfc_swiggy", "hdfc_infinia"]))
    assert r.status_code == 422 and "wallet too large" in r.json()["detail"]
    r = c.post("/api/optimize", json=body(amount=1001, wallet=["amazon_icici"]))
    assert r.status_code == 422 and "amount too large" in r.json()["detail"]
    r = c.post("/api/optimize", json=body(amount=0.001, wallet=["amazon_icici"]))
    assert r.status_code == 422                      # rounds to 0 paise: was a 500 before
    w = ["amazon_icici"]
    assert c.post("/api/optimize", json=body(wallet=w, usage={"amazon_icici": {"a": 1, "b": 2}})).status_code == 422
    assert c.post("/api/optimize", json=body(wallet=w, usage={"amazon_icici": {"a": -1}})).status_code == 422
    assert c.post("/api/optimize", json=body(wallet=w, usage={"amazon_icici": {"a": 1}, "hdfc_swiggy": {"b": 1}})).status_code == 422
    assert c.post("/api/optimize", content=b'{"transaction":{"amount":1e999,"mcc":"5814","channel":"online"},"wallet":["amazon_icici"]}',
                  headers={"content-type": "application/json"}).status_code == 422
    assert c.post("/api/optimize", json=body(wallet=w, merchant="x" * 65)).status_code == 422


def test_existing_422_for_unknown_cards_is_unchanged(make_client):
    r = make_client().post("/api/optimize", json=body(wallet=["nope"]))
    assert r.status_code == 422 and r.json() == {"detail": "unknown cards: ['nope']"}
