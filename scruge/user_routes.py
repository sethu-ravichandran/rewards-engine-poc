"""Per-user state, planning and reconciliation routes (registered by api.create_app)."""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from .catalog import card_on
from .forecast import forecast as make_forecast
from .offers import OfferBook
from .planner import first_move_advice
from .planner import plan as make_plan
from .presenter import Presenter, json_response, valid_cycle, valid_uid
from .reconcile import Credit, Expected
from .reconcile import reconcile as run_reconcile
from .schemas import CreditsIn, PlanIn, ReconcileIn, UserPlanIn, WalletIn


def _parse_time(raw: str | None, what: str = "as_of") -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{what} must be an ISO 8601 timestamp") from None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def register_user_routes(app: FastAPI, cfg, catalog, offers: dict, users, P: Presenter) -> None:
    def build_reconcile(uid: str, cycle: str | None, snap, as_of: datetime | None = None) -> dict:
        rows = users.get_ledger(uid, cycle)
        expected = [Expected(r["txn_ref"], r["card_id"], r["amount_paise"], r["expected_paise"], r["created_at"],
                             r["rule_id"], r["merchant"], r["mcc"], r["rules_version"]) for r in reversed(rows)]
        credits = [Credit(r["txn_ref"], r["actual_paise"], r["credited_at"]) for r in rows if r["actual_paise"] is not None]
        return run_reconcile(expected, credits, snap.cards, as_of, card_on=card_on)

    @app.get("/api/users/{uid}/state")
    async def user_state(uid: str, cycle: str | None = None) -> Response:
        valid_uid(uid); cyc = valid_cycle(cycle)

        def read():
            ledger = users.get_ledger(uid, cyc)
            return {"user_id": uid, "cycle": cyc, "exists": users.has_user(uid), "wallet": users.get_wallet(uid),
                    "cycle_usage_paise": users.get_usage(uid, cyc), "ledger": ledger,
                    "totals": {"count": len(ledger),
                               "expected_paise": sum(r["expected_paise"] for r in ledger),
                               "net_paise": sum(r["net_paise"] for r in ledger),
                               "offer_paise": sum(r["offer_paise"] for r in ledger),
                               "left_on_table_paise": sum(r["left_on_table_paise"] for r in ledger)}}
        return json_response(await run_in_threadpool(read))

    @app.put("/api/users/{uid}/wallet")
    async def user_wallet(uid: str, body: WalletIn) -> Response:
        valid_uid(uid)
        unknown = [c for c in body.cards if c not in catalog.get().cards]
        if unknown:
            raise HTTPException(422, f"unknown cards: {unknown}")
        cards = list(dict.fromkeys(body.cards))
        await run_in_threadpool(users.set_wallet, uid, cards)
        return json_response({"user_id": uid, "wallet": cards})

    @app.post("/api/users/{uid}/reset")
    async def user_reset(uid: str, cycle: str | None = None) -> Response:
        valid_uid(uid)
        await run_in_threadpool(users.reset, uid, valid_cycle(cycle))
        return json_response({"user_id": uid, "cycle": valid_cycle(cycle), "reset": True})

    @app.post("/api/users/{uid}/credits")
    async def user_credits(uid: str, body: CreditsIn, cycle: str | None = None) -> Response:
        """Store what the issuer actually credited (by txn_ref) and return the reconciliation report."""
        valid_uid(uid); cyc = valid_cycle(cycle); snap = catalog.get()

        def run():
            n, unmatched = users.record_credits(uid, [c.model_dump() for c in body.credits])
            return {"recorded": n, "unmatched_refs": unmatched, "report": build_reconcile(uid, cyc, snap)}
        return json_response(await run_in_threadpool(run))

    @app.post("/api/users/{uid}/statement")
    async def user_statement(uid: str, request: Request, cycle: str | None = None) -> Response:
        """Import an issuer statement as CSV (text/csv body). Columns: txn_ref, credited_paise (or credited_inr), credited_at (optional).
        Good rows are recorded and reconciled; bad rows are listed with their line number and never abort the rest."""
        valid_uid(uid); cyc = valid_cycle(cycle); snap = catalog.get()
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise HTTPException(413, "statement too large (max 1 MB)")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "statement must be UTF-8 text") from None
        rows = list(csv.DictReader(io.StringIO(text)))
        head = [h.strip().lower() for h in (csv.reader(io.StringIO(text)).__next__() if text.strip() else [])]
        if "txn_ref" not in head or not ({"credited_paise", "credited_inr"} & set(head)):
            raise HTTPException(422, "CSV needs a header with txn_ref and credited_paise (or credited_inr)")
        if len(rows) > 2000:
            raise HTTPException(422, "too many rows (max 2000)")
        counts: dict[str, int] = {}
        for r in rows:
            k = (r.get("txn_ref") or "").strip()
            counts[k] = counts.get(k, 0) + 1
        good, rejected = [], []
        for n, r in enumerate(rows, start=2):                      # line 1 is the header
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            ref = r.get("txn_ref", "")
            try:
                if not ref or len(ref) > 64:
                    raise ValueError("txn_ref is empty or longer than 64 characters")
                if counts[ref] > 1:
                    raise ValueError(f"txn_ref appears {counts[ref]} times in this file; reversals and split credits must be netted into one line before import")
                if r.get("credited_paise"):
                    if not re.fullmatch(r"\d{1,13}", r["credited_paise"]):
                        raise ValueError("credited_paise must be a whole number of paise")
                    paise = int(r["credited_paise"])
                elif r.get("credited_inr"):
                    if not re.fullmatch(r"\d{1,11}(\.\d{1,2})?", r["credited_inr"]):
                        raise ValueError("credited_inr must look like 123 or 123.45")
                    rupees, _, frac = r["credited_inr"].partition(".")
                    paise = int(rupees) * 100 + int((frac + "00")[:2])      # decimal text, never a float
                else:
                    raise ValueError("no credited amount")
                when = r.get("credited_at") or None
                if when:
                    try:
                        datetime.fromisoformat(when.replace("Z", "+00:00"))
                    except ValueError:
                        raise ValueError("credited_at must be an ISO date or time like 2026-11-20") from None
                good.append({"txn_ref": ref, "actual_paise": paise, "credited_at": when})
            except ValueError as e:
                rejected.append({"line": n, "txn_ref": ref, "error": str(e)})

        def run():
            n, unmatched = users.record_credits(uid, good) if good else (0, [])
            return {"rows": len(rows), "distinct_refs": len(counts), "recorded": n, "unmatched_refs": unmatched, "rejected": rejected,
                    "report": build_reconcile(uid, cyc, snap)}
        return json_response(await run_in_threadpool(run))

    @app.get("/api/users/{uid}/reconcile")
    async def user_reconcile(uid: str, cycle: str | None = None) -> Response:
        valid_uid(uid)
        return json_response(await run_in_threadpool(build_reconcile, uid, valid_cycle(cycle), catalog.get()))

    @app.post("/api/reconcile")
    async def reconcile_endpoint(body: ReconcileIn) -> Response:
        """Stateless reconciliation: expected rewards vs issuer credits, with a diagnosis for each mismatch."""
        try:
            as_of = _parse_time(body.as_of)
            expected = [Expected(e.txn_ref, e.card_id, e.amount_paise, e.expected_paise, e.created_at,
                                 e.rule_id, e.merchant) for e in body.expected]
            for e in expected:
                datetime.fromisoformat(e.created_at.replace("Z", "+00:00"))
        except (ValueError, HTTPException):
            raise HTTPException(422, "created_at and as_of must be ISO 8601 timestamps") from None
        credits = [Credit(c.txn_ref, c.actual_paise, c.credited_at) for c in body.credits]
        return json_response(run_reconcile(expected, credits, catalog.get().cards, as_of, body.settlement_days))

    def plan_out(result, wallet, snap, states) -> dict:
        names = {c.id: c.name for c in wallet}
        steps = [{"index": st.index, "card_id": st.card_id, "card": names.get(st.card_id), "value_paise": st.value_paise,
                  "greedy_card_id": gst.card_id, "greedy_value_paise": gst.value_paise, "differs": st.card_id != gst.card_id}
                 for st, gst in zip(result.steps, result.greedy_steps)]
        return {"total_paise": result.total_paise, "greedy_total_paise": result.greedy_total_paise,
                "gain_paise": result.gain_paise, "optimal": result.optimal, "nodes": result.nodes,
                "steps": steps, "first_move": first_move_advice(result, snap.cards, states)}

    @app.post("/api/plan")
    async def plan_endpoint(body: PlanIn) -> Response:
        """Choose cards for a sequence of payments, saving scarce caps for where they pay most.

        Never worse than paying greedily one at a time; `optimal` says whether the search finished inside the
        node budget (otherwise it is the best plan found so far).
        """
        if len(body.transactions) > cfg.plan_max_txns:
            raise HTTPException(422, f"too many transactions to plan: {len(body.transactions)} (max {cfg.plan_max_txns})")
        snap = catalog.get()
        wallet = P.wallet_of(snap, body.wallet, P.day_of(body.transactions))
        txns = [P.txn_of(t) for t in body.transactions]
        if body.user_id:
            usage = await run_in_threadpool(users.get_usage, valid_uid(body.user_id), valid_cycle(None))
        else:
            usage = P.usage_paise({}, body.cycle_usage_paise)
        states = P.states_of(usage)
        ousage = body.offer_usage_paise
        if body.user_id:
            ousage = await run_in_threadpool(users.get_offer_usage, valid_uid(body.user_id), valid_cycle(None))
        book = offers["book"] if body.include_offers else OfferBook((), "none")
        budget = min(body.node_budget or cfg.plan_node_budget, cfg.plan_node_budget * 4)
        result = await run_in_threadpool(make_plan, wallet, txns, states, book, budget, P.blocked if body.channel_rules else None, ousage)
        return json_response(plan_out(result, wallet, snap, states))

    @app.get("/api/users/{uid}/forecast")
    async def user_forecast(uid: str, as_of: str | None = None) -> Response:
        """What this user is likely to pay for during the rest of the cycle, from their own ledger."""
        valid_uid(uid)
        rows = await run_in_threadpool(users.get_ledger, uid, None, 500)
        return json_response(make_forecast(rows, _parse_time(as_of), cfg.plan_max_txns).as_dict())

    @app.post("/api/users/{uid}/plan")
    async def user_plan(uid: str, body: UserPlanIn) -> Response:
        """Plan the rest of the cycle from stored wallet, stored cap usage and a forecast of upcoming payments.

        The optional `transaction` is the payment happening now; the others are forecast from the ledger (see
        forecast.py). The forecast and its confidence are returned with the plan so the advice can be judged: a
        plan on a low-confidence forecast is a hint, not a promise.
        """
        valid_uid(uid); snap = catalog.get(); now = _parse_time(body.as_of)
        cyc = (now or datetime.now(timezone.utc)).strftime("%Y-%m")

        def load():
            return users.get_wallet(uid), users.get_ledger(uid, None, 500), users.get_usage(uid, cyc), users.get_offer_usage(uid, cyc)
        stored_wallet, rows, usage, ousage = await run_in_threadpool(load)
        ids = body.wallet or stored_wallet
        if not ids:
            raise HTTPException(422, "no wallet: send `wallet` or save one with PUT /api/users/{id}/wallet")
        wallet = P.wallet_of(snap, ids, body.transaction.on if body.transaction else None)
        current = P.txn_of(body.transaction) if body.transaction else None
        fc = make_forecast(rows, now, cfg.plan_max_txns - (1 if current else 0))
        txns = ([current] if current else []) + list(fc.transactions)
        out = {"forecast": fc.as_dict(), "current_included": current is not None}
        if not txns:
            return json_response({**out, "plan": None})
        states = P.states_of(usage)
        book = offers["book"] if body.include_offers else OfferBook((), "none")
        result = await run_in_threadpool(make_plan, wallet, txns, states, book, cfg.plan_node_budget, P.blocked if body.channel_rules else None, ousage)
        return json_response({**out, "plan": plan_out(result, wallet, snap, states)})
