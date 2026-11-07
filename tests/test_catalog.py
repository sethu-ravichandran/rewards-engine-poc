import json

import pytest

from scruge.catalog import CatalogError, CatalogStore, rules_version, validate_catalog
from tests.conftest import DATA, body, write_rules


def data():
    return json.loads(DATA.read_text())


def test_real_catalog_is_valid():
    validate_catalog(data())


def test_version_is_content_hash_independent_of_formatting():
    d = data()
    v = rules_version(d)
    assert v == rules_version(json.loads(json.dumps(d, indent=4, sort_keys=True)))
    d["cards"][0]["base_rate_bps"] += 1
    assert rules_version(d) != v


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.pop("cards"), "cards"),
    (lambda d: d["cards"].clear(), "cards"),
    (lambda d: d["cards"][0].pop("name"), "name"),
    (lambda d: d["cards"][0].update(network="bitcoin"), "network"),
    (lambda d: d["cards"][1].update(id=d["cards"][0]["id"]), "duplicate id"),
    (lambda d: d["cards"][0].update(id="a|b"), "id"),
    (lambda d: d["cards"][0].update(base_rate_bps="100"), "base_rate_bps"),
    (lambda d: d["cards"][0]["rules"][0].update(rate_bps=-5), "rate_bps"),
    (lambda d: d["cards"][0]["rules"][0].update(channels=["telepathy"]), "channel"),
    (lambda d: d["cards"][0]["rules"][0].update(cap_paise=0), "cap_paise"),
    (lambda d: d["cards"][0]["rules"][0].update(mccs=["12"]), "mccs"),
    (lambda d: d["cards"][0]["rules"].append(dict(d["cards"][0]["rules"][0])), "duplicate rule id"),
    (lambda d: d["cards"][0].update(surcharge_waiver={"fuel": [1]}), "surcharge_waiver"),
])
def test_invalid_catalogs_rejected(mutate, msg):
    d = data()
    mutate(d)
    with pytest.raises(CatalogError, match=msg):
        validate_catalog(d)


def test_store_refuses_invalid_reload_and_keeps_serving(rules_file):
    store = CatalogStore(rules_file, watch_interval_s=0)
    v1 = store.get().version
    rules_file.write_text("{ not json")
    with pytest.raises(CatalogError):
        store.reload()
    assert store.get().version == v1 and store.last_error


def test_store_fails_fast_on_invalid_startup(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"cards": []}')
    with pytest.raises(CatalogError):
        CatalogStore(p)


def test_hot_reload_swaps_snapshot_atomically(rules_file):
    store = CatalogStore(rules_file, watch_interval_s=0)
    old = store.get()
    write_rules(rules_file, lambda d: d["cards"][1].update(base_rate_bps=777))
    assert store.get() is old                   # watching disabled: nothing changes by itself
    assert store.reload() == "reloaded"
    new = store.get()
    assert new.version != old.version
    assert old.cards["amazon_icici"].base_rate_bps == 100   # old snapshot untouched (immutable)
    assert new.cards["amazon_icici"].base_rate_bps == 777
    assert store.reload() == "unchanged"


def test_file_watcher_picks_up_change_and_ignores_broken_file(rules_file):
    store = CatalogStore(rules_file, watch_interval_s=0.0001)
    v1 = store.get().version
    import time
    time.sleep(0.01)
    write_rules(rules_file, lambda d: d["cards"][0].update(base_rate_bps=123))
    time.sleep(0.01)
    v2 = store.get().version
    assert v2 != v1
    rules_file.write_text("garbage")
    time.sleep(0.01)
    assert store.get().version == v2          # invalid file: keep serving the last good rules
    assert store.last_error
    rules_file.write_text(json.dumps(data()))
    time.sleep(0.01)
    assert store.get().version == v1 and store.last_error is None   # recovers when fixed


def test_admin_reload_endpoint(make_client, rules_file):
    c = make_client(admin_token="s3cret")
    v1 = c.get("/api/meta").json()["rules_version"]
    write_rules(rules_file, lambda d: d["cards"][0].update(base_rate_bps=250))
    assert c.post("/api/admin/reload").status_code == 401
    assert c.post("/api/admin/reload", headers={"X-Admin-Token": "wrong"}).status_code == 401
    r = c.post("/api/admin/reload", headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 200 and r.json()["status"] == "reloaded"
    v2 = r.json()["rules_version"]
    assert v2 != v1 and c.get("/api/meta").json()["rules_version"] == v2
    assert c.post("/api/optimize", json=body()).headers["x-rules-version"] == v2
    # invalid file: 422, old rules keep serving
    rules_file.write_text('{"cards": [{"id": "x"}]}')
    r = c.post("/api/admin/reload", headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 422 and v2 in r.json()["detail"]
    assert c.get("/api/meta").json()["rules_version"] == v2
    assert c.post("/api/optimize", json=body()).status_code == 200


def test_admin_reload_disabled_without_token(make_client):
    assert make_client().post("/api/admin/reload", headers={"X-Admin-Token": "x"}).status_code == 404
