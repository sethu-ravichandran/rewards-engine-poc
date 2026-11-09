"""HTTP API + UI host. Run: uvicorn rewards_engine.api:app --reload

Core routes live here (rules, optimize, batch, commit, offers, health, the UI). The per-user, planning and
reconciliation routes are in user_routes.py; request bodies in schemas.py; validation and response shaping
in presenter.py. The engine itself stays a pure function of (rules, wallet, payment, cap usage).

Operations are deliberately minimal: request ids, Server-Timing, a JSON access log, a body-size limit, a
generic 500, health probes and rules hot-reload. There is no result cache, rate limiter or metrics registry:
none earned its place at this size.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from pydantic import ValidationError

from .catalog import DEFAULT_PATH, CatalogError, CatalogStore
from .channels import guidance
from .compare import register_compare_routes
from .config import Settings
from .engine import consume, evaluate, optimize
from .models import CycleState
from .obs import EdgeMiddleware, configure_logging
from .offers import DEFAULT_OFFERS_PATH, OfferBook, OfferError, with_offer
from .presenter import (
    Presenter,
    dumps,
    json_response,
    usage_out,
    usage_paise_out,
    valid_cycle,
    valid_uid,
)
from .schemas import BatchIn, CommitIn, OptimizeIn
from .user_routes import register_user_routes
from .userstore import IdempotencyConflict, UserStore

WEB = Path(__file__).resolve().parent.parent / "web"
JSON = "application/json"
log = logging.getLogger("rewards_engine.api")


def _accepts_gzip(request: Request) -> bool:
    for part in request.headers.get("accept-encoding", "").split(","):
        tok, _, q = part.strip().partition(";")
        if tok.strip().lower() == "gzip":
            return q.strip().replace(" ", "") not in ("q=0", "q=0.0", "q=0.00", "q=0.000")
    return False


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    return any(t.strip().removeprefix("W/") == etag for t in header.split(","))


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def create_app(settings: Settings | None = None, rules_path: str | Path | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()
    configure_logging(cfg.log_level)
    catalog = CatalogStore(rules_path or cfg.rules_path or DEFAULT_PATH, cfg.rules_watch_interval_s)
    offers_path = Path(cfg.offers_path) if cfg.offers_path else DEFAULT_OFFERS_PATH
    offers: dict[str, OfferBook] = {"book": OfferBook.load(offers_path)}      # a holder: a reload swaps the book atomically
    # a serverless filesystem is read-only, so on Vercel the user store lives in memory
    default_db = ":memory:" if os.environ.get("VERCEL") else Path(__file__).resolve().parent.parent / "data" / "users.sqlite3"
    users = UserStore(cfg.user_db or default_db)
    P = Presenter(cfg, offers)

    app = FastAPI(title="Rewards Engine (PoC)")

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> Response:
        # FastAPI echoes the offending input; NaN/Infinity in it cannot be encoded as JSON and used to become a 500.
        def clean(v):
            if isinstance(v, float) and v != v or v in (float("inf"), float("-inf")):
                return str(v)
            if isinstance(v, dict):
                return {k: clean(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [clean(x) for x in v]
            return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
        return Response(json.dumps({"detail": clean(exc.errors())}), status_code=422, media_type=JSON)
    app.state.settings, app.state.catalog, app.state.users, app.state.offers = cfg, catalog, users, offers

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            snap = catalog.get()
            return json_response({"status": "ready", "rules_version": snap.version, "cards": len(snap.cards)})
        except Exception:
            return json_response({"status": "not ready"}, 503)

    @app.get("/api/meta")
    async def meta(request: Request) -> Response:
        snap = catalog.get()
        h = {"ETag": snap.meta_etag, "Cache-Control": f"public, max-age={cfg.meta_max_age_s}", "Vary": "Accept-Encoding"}
        if _etag_matches(request.headers.get("if-none-match"), snap.meta_etag):
            return Response(status_code=304, headers=h)
        if len(snap.meta_body) >= cfg.gzip_min_bytes and _accepts_gzip(request):
            return Response(snap.meta_gzip, media_type=JSON, headers={**h, "Content-Encoding": "gzip"})
        return Response(snap.meta_body, media_type=JSON, headers=h)

    @app.get("/api/offers")
    async def offers_endpoint() -> Response:
        book = offers["book"]
        return json_response({"version": book.version, "offers": [
            {"id": o.id, "label": o.label, "kind": o.kind, "merchants": sorted(o.merchants), "cards": sorted(o.cards),
             "channels": sorted(c.value for c in o.channels), "discount_bps": o.discount_bps, "cap_paise": o.cap_paise,
             "cycle_cap_paise": o.cycle_cap_paise,
             "min_txn_paise": o.min_txn_paise, "source": o.source, "verified_on": o.verified_on, "confidence": o.confidence}
            for o in book.offers]})

    @app.post("/api/admin/reload")
    async def admin_reload(x_admin_token: str | None = Header(None)) -> Response:
        """Re-read the rules and offers files now. Disabled (404) unless ADMIN_TOKEN is set."""
        if not cfg.admin_token:
            raise HTTPException(404, "not found")
        if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), cfg.admin_token.encode()):
            raise HTTPException(401, "invalid admin token")
        try:
            result = await run_in_threadpool(catalog.reload, True)
        except CatalogError as e:
            raise HTTPException(422, f"rules file rejected, still serving {catalog.get().version}: {e}") from None
        try:  # offers reload independently; an invalid offers file keeps the previous book serving
            offers["book"] = await run_in_threadpool(OfferBook.load, offers_path)
        except (OfferError, ValueError, OSError) as e:
            raise HTTPException(422, f"offers file rejected, still serving {offers['book'].version}: {e}") from None
        return json_response({"status": result, "rules_version": catalog.get().version, "offers_version": offers["book"].version})

    # This endpoint parses its own body: FastAPI's generic body handling cost ~25us per request against ~2us for
    # pydantic directly. 422s keep FastAPI's shape and /docs still shows the schema. `?explain=false` skips `why`.
    @app.post("/api/optimize", openapi_extra={"requestBody": {"required": True, "content": {
        JSON: {"schema": OptimizeIn.model_json_schema()}}}})
    async def optimize_endpoint(request: Request) -> Response:
        raw = await request.body()
        try:
            try:
                parsed = json.loads(raw, parse_constant=_reject_constant) if raw else None
            except ValueError as e:
                raise RequestValidationError([{"type": "json_invalid", "loc": ("body", getattr(e, "pos", 0)),
                                               "msg": "JSON decode error", "input": {},
                                               "ctx": {"error": getattr(e, "msg", str(e))}}]) from None
            if parsed is None:  # empty body / JSON null: FastAPI reports a missing required body
                raise RequestValidationError([{"type": "missing", "loc": ("body",), "msg": "Field required", "input": None}])
            body = OptimizeIn.model_validate(parsed, from_attributes=True)  # as FastAPI does (same messages)
        except ValidationError as e:
            raise RequestValidationError(
                [{**err, "loc": ("body", *err["loc"])} for err in e.errors(include_url=False)]) from None
        explain = request.query_params.get("explain", "true").lower() not in ("false", "0", "no", "off")
        wallet = P.wallet_of(catalog.get(), body.wallet, body.transaction.on)
        txn = P.txn_of(body.transaction)
        usage = P.usage_paise(body.cycle_usage, body.cycle_usage_paise)
        lean = body.transaction.amount_paise is not None   # response shape follows request shape
        rec = optimize(wallet, txn, P.states_of(usage), explain=explain)
        best, outs = P.present(rec, txn, body.include_offers, explain, lean, body.channel_rules, body.offer_usage_paise)
        result = {"best": best, "ranked": outs}
        if body.channel_rules:      # advice only when it applies, so the response keeps its usual shape otherwise
            why_not = P.blocked(txn)
            advice = [why_not] if why_not else guidance(wallet, txn, best["total_paise"] if best else None, best["card_id"] if best else None)
            if advice:
                result["advice"] = advice
        return Response(dumps(result), media_type=JSON)

    def run_batch(snap, body: BatchIn) -> bytes:
        wallet = P.wallet_of(snap, body.wallet, P.day_of(body.transactions))
        usage = P.usage_paise(body.cycle_usage, body.cycle_usage_paise)
        txns = [P.txn_of(t) for t in body.transactions]
        lean = body.transactions[0].amount_paise is not None
        states = P.states_of(usage)
        ousage = dict(body.offer_usage_paise)
        results = []
        tot_reward = tot_surcharge = tot_net = tot_offer = 0
        for txn in txns:
            rec = optimize(wallet, txn, states, explain=body.explain)
            best, outs = P.present(rec, txn, body.include_offers, body.explain, lean, body.channel_rules, ousage)
            if best is not None:
                tot_reward += best["reward_paise"]; tot_surcharge += best["surcharge_paise"]
                tot_net += best["net_paise"]; tot_offer += best["offer_paise"]
                if body.sequential:
                    if best["offer"]:
                        ousage[best["offer"]["id"]] = ousage.get(best["offer"]["id"], 0) + best["offer_paise"]
                    ev = next(e for e in rec.ranked if e.card_id == best["card_id"])
                    consume(snap.cards[best["card_id"]], txn, states.setdefault(best["card_id"], CycleState()), ev)
            results.append({"best": best, "ranked": outs})
        totals = {"reward_paise": tot_reward, "surcharge_paise": tot_surcharge, "net_paise": tot_net,
                  "offer_paise": tot_offer, "total_paise": tot_net + tot_offer}
        if not lean:
            totals.update(reward_inr=tot_reward / 100, surcharge_inr=tot_surcharge / 100, net_inr=tot_net / 100)
        result = {"count": len(results), "sequential": body.sequential, "results": results, "totals": totals}
        if body.sequential:
            result["cycle_usage_paise"] = usage_paise_out(states)
            if not lean:
                result["cycle_usage"] = usage_out(states)
        return dumps(result)

    @app.post("/api/optimize/batch")
    async def optimize_batch(body: BatchIn, request: Request) -> Response:
        if len(body.transactions) > cfg.max_batch:
            raise HTTPException(422, f"too many transactions: {len(body.transactions)} (max {cfg.max_batch})")
        snap = catalog.get()
        # Large batches can run for milliseconds: keep them off the event loop.
        payload = await run_in_threadpool(run_batch, snap, body) if len(body.transactions) > 10 else run_batch(snap, body)
        if len(payload) >= cfg.gzip_min_bytes and _accepts_gzip(request):
            return Response(gzip.compress(payload, 3, mtime=0), media_type=JSON,
                            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(payload, media_type=JSON, headers={"Vary": "Accept-Encoding"})

    @app.post("/api/commit")
    async def commit_endpoint(body: CommitIn) -> Response:
        """Record a spend: applies it to the chosen card's caps and returns the new usage.

        Two modes. Stateless: the caller sends `cycle_usage_paise` and gets the updated usage back.
        Persistent: with `user_id` (and a `txn_ref`) the server loads the user's usage for the cycle, applies the
        payment, updates usage and appends a ledger row in ONE transaction. A retry with the same txn_ref returns
        the stored response and applies nothing twice (`Idempotent-Replay: true`); the same txn_ref for a
        different request is a 409.
        """
        snap = catalog.get()
        wallet = P.wallet_of(snap, body.wallet, body.transaction.on)
        txn = P.txn_of(body.transaction)
        lean = body.transaction.amount_paise is not None

        def compute(usage: dict[str, dict[str, int]], ousage: dict[str, int]):
            states = P.states_of(usage)
            rec = optimize(wallet, txn, states)
            if body.channel_rules and P.blocked(txn):
                raise HTTPException(422, P.blocked(txn))
            best, outs = P.present(rec, txn, body.include_offers, True, lean, body.channel_rules, ousage)
            chosen_id = body.card_id or (best["card_id"] if best else None)
            if chosen_id is None:
                raise HTTPException(422, "no eligible card for this transaction")
            card = next((c for c in wallet if c.id == chosen_id), None)
            if card is None:
                raise HTTPException(422, f"{chosen_id} not in wallet")
            ev = evaluate(card, txn, states.setdefault(card.id, CycleState()), True)
            if not ev.eligible:
                raise HTTPException(422, f"{card.name} cannot be used for this transaction")
            used = next(o for o in outs if o["card_id"] == card.id)
            if (used.get("offer") or {}).get("kind") == "voucher":   # a voucher purchase earns no card reward and uses no cap
                ev, _ = with_offer(ev, offers["book"].best_for(card.id, txn, ousage))
            else:
                consume(card, txn, states[card.id], ev)
            left = max(0, (best["total_paise"] if best else 0) - used["total_paise"])
            payload = {"used": used, "best": best, "left_on_table_paise": left, "cycle_usage_paise": usage_paise_out(states)}
            if not lean:
                payload.update(left_on_table_inr=left / 100, cycle_usage=usage_out(states))
            row = {"created_at": (f"{body.transaction.on}T12:00:00+00:00" if body.transaction.on else datetime.now(timezone.utc).isoformat()), "merchant": txn.merchant, "mcc": txn.mcc,
                   "channel": txn.channel.value, "amount_paise": txn.amount_paise, "card_id": card.id,
                   "card_name": card.name, "rule_id": ev.rule_id, "expected_paise": ev.reward_paise,
                   "surcharge_paise": ev.surcharge_paise, "net_paise": ev.net_paise,
                   "offer_paise": used["offer_paise"], "offer_id": (used["offer"] or {}).get("id"),
                   "left_on_table_paise": left, "rules_version": snap.version}
            return usage_paise_out(states), row, payload

        if not body.user_id:
            return json_response(compute(P.usage_paise(body.cycle_usage, body.cycle_usage_paise), body.offer_usage_paise)[2])
        uid, cycle, ref = valid_uid(body.user_id), valid_cycle(body.cycle or (body.transaction.on[:7] if body.transaction.on else None)), body.txn_ref or uuid.uuid4().hex

        def run(usage):                      # stored usage wins: the client cannot rewind its own caps
            new_usage, row, payload = compute(usage, users.get_offer_usage(uid, cycle))   # stored offer usage wins too
            row["txn_ref"] = ref
            return new_usage, row, {**payload, "txn_ref": ref, "cycle": cycle, "persisted": True}
        fp = hashlib.sha256(body.model_dump_json(exclude={"cycle_usage", "cycle_usage_paise", "user_id", "txn_ref"}).encode()).hexdigest()
        try:
            payload, replayed = await run_in_threadpool(users.commit, uid, cycle, ref, run, fp)
        except IdempotencyConflict:
            raise HTTPException(409, "txn_ref was already used with a different request body") from None
        return json_response(payload, headers={"Idempotent-Replay": "true"} if replayed else None)

    register_user_routes(app, cfg, catalog, offers, users, P)
    register_compare_routes(app, catalog, offers, P)

    @app.get("/")
    async def index() -> FileResponse:
        # always revalidate (a 304 is cheap) so a new UI shows up immediately
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.add_middleware(EdgeMiddleware, get_version=lambda: catalog._snap.version,
                       max_body_bytes=cfg.max_body_bytes, access_log_enabled=cfg.access_log)
    return app


app = create_app()
