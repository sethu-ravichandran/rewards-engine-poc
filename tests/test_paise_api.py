"""Money on the wire is integer paise; the rupee floats are deprecated display fields."""
from tests.conftest import body


def _paise(amount_paise, **kw):
    b = body(**kw)
    b["transaction"].pop("amount")
    b["transaction"]["amount_paise"] = amount_paise
    return b


def _same_values(legacy: dict, lean: dict) -> bool:
    """The lean (paise) response is the legacy response minus the deprecated float fields: same values everywhere else."""
    if isinstance(lean, dict):
        return all(k in legacy and _same_values(legacy[k], v) for k, v in lean.items())
    if isinstance(lean, list):
        return len(lean) == len(legacy) and all(_same_values(a, b) for a, b in zip(legacy, lean))
    return legacy == lean


def _floats(o):
    if isinstance(o, float):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _floats(v)
    elif isinstance(o, list):
        for v in o:
            yield from _floats(v)


def test_response_shape_follows_request_shape(make_client):
    """Paise request -> lean, integer-only response. Legacy `amount` request -> the old fields too."""
    c = make_client()
    legacy = c.post("/api/optimize", json=body(amount=1000)).json()
    lean = c.post("/api/optimize", json=_paise(100_000)).json()
    assert _same_values(legacy, lean) and lean != legacy
    assert not list(_floats(lean)), "a paise request must get an integer-only response"
    assert not any(k.endswith("_inr") or k == "effective_rate_pct" for e in lean["ranked"] for k in e)
    assert all("net_inr" in e and "effective_rate_pct" in e for e in legacy["ranked"])      # old clients unaffected
    assert lean["best"]["effective_rate_bps"] == 1000 and legacy["best"]["effective_rate_pct"] == 10.0


def test_amount_paise_matches_legacy_amount(make_client):
    c = make_client()
    a = c.post("/api/optimize", json=body(amount=1000)).json()
    b = c.post("/api/optimize", json=_paise(100_000)).json()
    assert _same_values(a, b)
    assert b["best"]["reward_paise"] == 10_000 and b["best"]["net_paise"] == 10_000
    assert all(isinstance(e["reward_paise"], int) and isinstance(e["net_paise"], int) for e in b["ranked"])


def test_amount_and_amount_paise_must_agree(make_client):
    c = make_client()
    b = body(amount=1000); b["transaction"]["amount_paise"] = 100_000
    assert c.post("/api/optimize", json=b).status_code == 200            # consistent: accepted
    b["transaction"]["amount_paise"] = 100_001
    assert c.post("/api/optimize", json=b).status_code == 422            # disagree: rejected, not guessed


def test_amount_is_required_in_some_form(make_client):
    b = body(); b["transaction"].pop("amount")
    assert make_client().post("/api/optimize", json=b).status_code == 422


def test_deprecated_float_goes_through_decimal_not_binary_math(make_client):
    c = make_client()
    # 19.99 * 100 is 1998.9999999999998 in binary floating point; the decimal path must give exactly 1999
    r = c.post("/api/optimize", json=body(amount=19.99, wallet=["hdfc_infinia"], merchant=None, mcc="5411", channel="tap")).json()
    p = c.post("/api/optimize", json=_paise(1999, wallet=["hdfc_infinia"], merchant=None, mcc="5411", channel="tap")).json()
    assert _same_values(r, p)
    # floor(1999 * 3.30%) = 65 paise: the engine's own integer rounding, unchanged
    assert p["best"]["reward_paise"] == 65


def test_non_positive_and_huge_paise_rejected(make_client):
    c = make_client(max_amount_inr=1000)
    assert c.post("/api/optimize", json=_paise(0)).status_code == 422
    assert c.post("/api/optimize", json=_paise(-5)).status_code == 422
    assert c.post("/api/optimize", json=_paise(100_001)).status_code == 422
    assert c.post("/api/optimize", json=_paise(100_000)).status_code == 200


def test_cycle_usage_paise_changes_the_winner(make_client):
    c = make_client()
    full = c.post("/api/optimize", json=_paise(100_000, wallet=["hdfc_swiggy", "hdfc_infinia"])).json()
    assert full["best"]["card_id"] == "hdfc_swiggy"
    b = _paise(100_000, wallet=["hdfc_swiggy", "hdfc_infinia"]); b["cycle_usage_paise"] = {"hdfc_swiggy": {"swiggy": 150_000}}
    assert c.post("/api/optimize", json=b).json()["best"]["card_id"] == "hdfc_infinia"


def test_usage_paise_and_deprecated_usage_cannot_be_mixed(make_client):
    b = _paise(100_000); b["cycle_usage_paise"] = {"hdfc_swiggy": {"swiggy": 1}}; b["cycle_usage"] = {"hdfc_swiggy": {"swiggy": 0.01}}
    assert make_client().post("/api/optimize", json=b).status_code == 422


def test_usage_paise_must_be_non_negative_ints(make_client):
    c = make_client()
    for bad in (-1, 1.5, "x"):
        b = _paise(100_000); b["cycle_usage_paise"] = {"hdfc_swiggy": {"swiggy": bad}}
        assert c.post("/api/optimize", json=b).status_code == 422


def test_commit_returns_paise_usage_and_left_on_table(make_client):
    c = make_client()
    b = _paise(100_000, wallet=["hdfc_swiggy", "hdfc_infinia"]); b["card_id"] = "hdfc_infinia"
    r = c.post("/api/commit", json=b).json()
    assert r["used"]["card_id"] == "hdfc_infinia"
    assert r["left_on_table_paise"] == 10_000 - 3_300 and "left_on_table_inr" not in r and not list(_floats(r))
    legacy = c.post("/api/commit", json={**body(amount=1000, wallet=["hdfc_swiggy", "hdfc_infinia"]), "card_id": "hdfc_infinia"}).json()
    assert legacy["left_on_table_inr"] == 67.0 and legacy["left_on_table_paise"] == 6_700
    assert not any(r["cycle_usage_paise"].values())           # Infinia's reward here is uncapped: no cap consumed
    b2 = _paise(100_000, wallet=["hdfc_swiggy"])
    assert c.post("/api/commit", json=b2).json()["cycle_usage_paise"] == {"hdfc_swiggy": {"swiggy": 10_000}}


def test_batch_totals_in_paise(make_client):
    c = make_client()
    r = c.post("/api/optimize/batch", json={"transactions": [{"amount_paise": 100_000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}] * 3,
                                            "wallet": ["hdfc_swiggy"], "sequential": True}).json()
    assert r["totals"]["reward_paise"] == 30_000 and r["totals"]["total_paise"] == 30_000
    assert r["cycle_usage_paise"] == {"hdfc_swiggy": {"swiggy": 30_000}}


def test_meta_exposes_paise_caps_and_provenance(make_client):
    m = make_client().get("/api/meta").json()
    sw = next(c for c in m["cards"] if c["id"] == "hdfc_swiggy")
    assert sw["caps_paise"] == {"swiggy": 150_000, "online_5": 150_000}
    assert sw["provenance"]["confidence"] == "illustrative" and sw["provenance"]["source"].startswith("https://")


def test_missing_and_conflicting_amounts_are_clear_422s(make_client):
    c = make_client()
    b = body(); b["transaction"].pop("amount")
    r = c.post("/api/optimize", json=b)
    assert r.status_code == 422 and "amount_paise is required" in r.text
    b = body(amount=1000); b["transaction"]["amount_paise"] = 100_001
    r = c.post("/api/commit", json=b)
    assert r.status_code == 422 and "disagree" in r.text
