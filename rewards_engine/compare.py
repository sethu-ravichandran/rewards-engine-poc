"""Comparison routes: which way to pay (Checkout, Card, Tap, UPI) and which marketplace is really cheapest.

Both run the existing engine and offer book per option; nothing here changes how a card is priced.
* /api/compare/routes: the same payment through every channel, plus "bank account via UPI" (no card at all) where a
  UPI offer pays on any account. Answers "is UPI better than my card for this payment?".
* /api/compare/prices: one product listed on several marketplaces. The sticker price is not the answer; the price
  after your best card's reward and the offer is.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from .catalog import MERCHANTS
from .engine import optimize
from .models import Channel, Transaction
from .presenter import Presenter, json_response
from .schemas import CompareIn, PriceIn

PRICES_PATH = Path(__file__).resolve().parent.parent / "data" / "prices.json"
ROUTES = (Channel.ONLINE, Channel.CARD, Channel.TAP, Channel.UPI)
BANK = "bank_account"


def load_prices(path: Path = PRICES_PATH) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"items": []}


def register_compare_routes(app: FastAPI, catalog, offers, P: Presenter) -> None:
    def best_for(wallet, txn: Transaction, ousage):
        """Best option for one transaction as (name, card_id, reward, offer, total, reason) or a block reason."""
        why = P.blocked(txn)
        if why:
            return {"available": False, "reason": why}
        rec = optimize(wallet, txn, {}, explain=False)
        best, _ = P.present(rec, txn, True, False, True, True, ousage)
        pick = None
        if best:
            pick = {"name": best["card"], "card_id": best["card_id"], "reward_paise": best["reward_paise"],
                    "surcharge_paise": best["surcharge_paise"], "offer_paise": best["offer_paise"],
                    "offer": (best.get("offer") or {}).get("label"), "total_paise": best["total_paise"]}
        if txn.channel is Channel.UPI:        # a plain bank account earns no card reward but can still get a UPI offer
            a = offers["book"].best_for(BANK, txn, ousage)
            if a and (pick is None or a.paise > pick["total_paise"]):
                pick = {"name": "Bank account (UPI)", "card_id": None, "reward_paise": 0, "surcharge_paise": 0,
                        "offer_paise": a.paise, "offer": a.offer.label, "total_paise": a.paise}
        if pick is None:
            return {"available": False, "reason": "None of the cards in your wallet can pay this way."}
        return {"available": True, **pick}

    @app.post("/api/compare/routes")
    async def compare_routes(body: CompareIn) -> Response:
        wallet = P.wallet_of(catalog.get(), body.wallet, body.transaction.on)
        base = P.txn_of(body.transaction)
        ousage = dict(body.offer_usage_paise)
        rows = []
        for ch in ROUTES:
            t = Transaction(base.amount_paise, base.mcc, ch, base.merchant)
            rows.append({"channel": ch.value, **best_for(wallet, t, ousage)})
        ok = [r for r in rows if r["available"]]
        win = max(ok, key=lambda r: (r["total_paise"], r["channel"] != "upi")) if ok else None   # ties prefer a card route
        for r in rows:
            r["winner"] = r is win
            if win and r["available"]:
                r["behind_paise"] = win["total_paise"] - r["total_paise"]
        return json_response({"amount_paise": base.amount_paise, "merchant": base.merchant, "routes": rows,
                              "winner": win["channel"] if win else None})

    @app.post("/api/compare/prices")
    async def compare_prices(body: PriceIn) -> Response:
        item = next((i for i in load_prices()["items"] if i["id"] == body.item), None)
        if item is None:
            raise HTTPException(404, f"unknown item {body.item!r}")
        wallet = P.wallet_of(catalog.get(), body.wallet, body.on)
        rows = []
        for ls in item["listings"]:
            m = ls["merchant"]
            name, mcc = MERCHANTS.get(m, (m, "5311"))
            t = Transaction(ls["price_paise"], mcc, Channel.ONLINE, m)
            r = best_for(wallet, t, {})
            eff = ls["price_paise"] - r["total_paise"] if r["available"] else ls["price_paise"]
            rows.append({"merchant": m, "merchant_name": name, "price_paise": ls["price_paise"],
                         "effective_paise": eff, **{k: r[k] for k in ("name", "reward_paise", "offer_paise", "offer", "total_paise") if k in r}})
        rows.sort(key=lambda r: (r["effective_paise"], r["merchant"]))
        cheapest_sticker = min(rows, key=lambda r: (r["price_paise"], r["merchant"]))
        for r in rows:
            r["cheapest"] = r is rows[0]
        return json_response({"item": item["id"], "name": item["name"], "listings": rows,
                              "cheapest_sticker": cheapest_sticker["merchant"],
                              "differs": cheapest_sticker["merchant"] != rows[0]["merchant"],
                              "saves_vs_sticker_paise": cheapest_sticker["effective_paise"] - rows[0]["effective_paise"],
                              "note": "Illustrative prices; not live."})

    @app.get("/api/compare/items")
    async def compare_items() -> Response:
        return json_response({"items": [{"id": i["id"], "name": i["name"]} for i in load_prices()["items"]]})
