"""The rules data itself: a rule that can never pay is a data bug."""
from scruge.catalog import load_cards
from scruge.engine import evaluate
from scruge.models import Channel, Transaction


def test_no_rule_pays_less_than_its_cards_base_rate():
    for c in load_cards().values():
        for r in c.rules:
            assert r.rate_bps >= c.base_rate_bps, f"{c.id}.{r.id} ({r.rate_bps} bps) is below the base rate ({c.base_rate_bps}) and can never pay"


def test_tata_neu_earns_less_on_upi_than_off_it():
    """The issuer page says 1.5% applies to non-UPI spends, so UPI earns less: modelled as a 0.5% base + a 1.5% non-UPI rule."""
    tata = load_cards()["tata_neu_rupay"]
    pay = lambda ch, m=None, mcc="5499": evaluate(tata, Transaction(1_000_000, mcc, Channel(ch), m), None, explain=False).reward_paise
    assert pay("upi") == 5_000 and pay("online") == pay("card") == pay("tap") == 15_000          # Rs 10,000: 0.5% vs 1.5%
    assert pay("online", "bigbasket", "5411") == 50_000 and pay("upi", "bigbasket", "5411") == 15_000   # partner brands: 5% off UPI, 1.5% on UPI
