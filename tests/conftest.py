import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rewards_engine.api import create_app
from rewards_engine.config import Settings

DATA = Path(__file__).resolve().parent.parent / "data" / "cards.json"
WALLET = ["amazon_icici", "hdfc_swiggy", "hdfc_infinia"]


@pytest.fixture
def rules_file(tmp_path):
    p = tmp_path / "cards.json"
    shutil.copy(DATA, p)
    return p


@pytest.fixture
def make_client(rules_file):
    """make_client(**settings) -> TestClient on a fresh app with its own copy of the rules file."""
    def _make(**kw):
        kw.setdefault("rules_watch_interval_s", 0.0)
        kw.setdefault("access_log", False)
        kw.setdefault("user_db", ":memory:")
        app = create_app(Settings(**kw), rules_path=rules_file)
        return TestClient(app)
    return _make


def body(amount=1000, mcc="5814", channel="online", merchant="swiggy", wallet=None, usage=None, **kw):
    t = {"amount": amount, "mcc": mcc, "channel": channel}
    if merchant:
        t["merchant"] = merchant
    b = {"transaction": t, "wallet": wallet or list(WALLET)}
    if usage:
        b["cycle_usage"] = usage
    b.update(kw)
    return b


def write_rules(path: Path, mutate):
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data, indent=1))
