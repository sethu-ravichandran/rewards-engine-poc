# SC₹UGE: pay with the card, UPI route or offer that keeps the most rupees (proof of work)

Given a payment and the cards a person holds, pick the card that earns the most and say why. Rules and offers are illustrative until verified against the issuer.
This is the decision at the heart of Scruge's four agents (**Checkout, Card, Tap, UPI**), built as a working service and app.

**Try it in 60 seconds** (`pip install -r requirements-dev.txt && uvicorn scruge.api:app`, open http://127.0.0.1:8000):

1. Pick **Amazon**, set **₹6,000**. The *Four agents* panel shows each agent's choice and a one-line reason. Checkout picks Swiggy HDFC for ₹900
   ("5.00% rewards + 10.00% offer = 15.00% total"); Card and Tap say Amazon is online-only; UPI picks the only RuPay card. Switch to **DMart** at ₹4,000: Card and Tap
   both pick Infinia, but Tap is worth ₹200 more because of an in-store tap offer. Above ₹5,000 a tap needs a PIN, so the app sends you to Card.
2. Pick **Petrol pump** at ₹6,000: every card loses ₹60 to the fuel surcharge, and the app says so and tells you to split it into two payments (the surcharge is waived per payment inside a band).
   **Rent** says to use a bank transfer or UPI instead.
3. Press **Run a month of spends**. Caps drain, the winner changes mid-month, and a **Look ahead** note says whether planning the month together would have beaten paying one at a time. In this month
   (festive shopping in the middle) it would have gained **₹500.80**. The search is exhaustive when it finishes within its node budget, and every answer says whether it did (`optimal`); here it did. The note counts the payments in *this run*, and the rewards and offer savings are shown separately, because a discount is not an issuer reward. **Plan ahead** forecasts the rest of your month from your own history (with its confidence stated), or plans an example of five festive payments
   (+₹425) when you have none. With a few payments in, the verdict also shows a **Planner tip** when holding a card's cap for later is better.
4. Press **Check credits (demo)**. A simulated issuer statement credits one payment at the base rate; the app finds the shortfall, says which rule the issuer probably applied, and drafts the dispute note.
5. Refresh the page. Your wallet, cap usage and payments are still there (saved server-side per browser).

## What is in it

| Piece | What it does | Where |
|---|---|---|
| **Engine** | Rules matched by merchant > MCC > category > channel; per-cycle caps (overflow earns base rate); exclusions; surcharges with waiver windows; channel rules (UPI is RuPay-only); an explanation for every result | `scruge/engine.py`, `models.py` |
| **Offers** | Instant discounts (bank offers, vouchers) with a per-payment cap, a minimum order and a **per-user per-cycle limit** (tracked in the ledger, so the sixth ₹90 voucher is the last full one and the eighth pays nothing), online or in-store-tap. Kept *outside* the engine so reward maths and its tests are untouched, and so predicted rewards stay comparable to what the issuer credits | `scruge/offers.py`, `data/offers.json` |
| **Channel rules + guidance** | Online-only merchants cannot be tapped or swiped; contactless above ₹5,000 needs a PIN; in-store tap offers make Tap different from Card; a payment that loses money gets advice (split a petrol fill into the waiver band, use a bank transfer for rent). Opt-in (`channel_rules`), outside the engine | `scruge/channels.py` |
| **Lookahead planner** | Chooses cards for a *sequence* of payments so scarce caps are saved for where they pay most. Exact branch-and-bound that starts from greedy, so it is never worse, and says whether it finished | `scruge/planner.py` |
| **Forecast** | Predicts the rest of the cycle from the user's own ledger (recurring merchant / channel patterns, typical amounts), with a stated confidence, so the planner needs no input from the caller | `scruge/forecast.py` |
| **Reward reconciliation** | Compares the reward we predicted with what the issuer credited; classifies each payment, diagnoses which rate the issuer probably applied, drafts a dispute note | `scruge/reconcile.py` |
| **Rules pipeline** | `build / lint / diff / publish / rollback / history` CLI with a review gate and content-addressed releases | `scruge/rules.py` |
| **Per-user state** | Wallet, cap usage per statement cycle and a payments ledger in SQLite; atomic, idempotent commits | `scruge/userstore.py` |
| **App** | The UI: wallet, four agents, 3D best-card, ranked cards with reasons, caps, payments, planner and reconciliation demos | `web/index.html` |

Card rules are **illustrative approximations** of popular Indian cards, not read from issuer T&Cs. Every card says so (`confidence`, `source`,
`verified_on` in `data/cards.json`), the API returns it, and the UI shows it under every recommendation. Offers are illustrative too.

## Money

Integer **paise** end to end: in the engine, in the API (`amount_paise`, `cycle_usage_paise`, every `*_paise` field) and in the UI (the typed
text is parsed to paise; a binary float never multiplies a rupee amount). Rewards round **down** (floor), as issuers do. The older rupee
float fields (`amount`, `cycle_usage`, `*_inr`) remain for backward compatibility; they are derived from paise, display-only and
deprecated. A deprecated float input is converted through its decimal text (`Decimal`), never `x * 100`. If `amount` and `amount_paise` disagree the request is rejected, not guessed.

## Questions this should answer

**Why did the API take floats?** It started as a quick, human-friendly contract (`"amount": 2500`) over an engine that was integer from day one,
and that mismatch is a real defect: `19.99 * 100` is `1998.9999999999998`, JSON round-trips floats through text, and rupee amounts with
paise cannot be represented exactly. The fix is the contract above: paise on the wire, a rejection when both forms disagree, and tests
(`tests/test_paise_api.py`) that pin the `19.99` case and the "floor at the paisa" rounding. Old clients keep working; new ones should send paise.

**How do 300 cards get in, and out?** Per-card files (`data/cards.d/<id>.json`) are merged by `build`; one card per file means a reviewable diff per
card and no merge conflicts between people editing different cards. `lint` blocks malformed rules and warns on missing sources, stale
verification dates, rates that look like a points-vs-rupees mix-up, and shared cap buckets with different caps. `diff` says what changed in plain
terms and **requires `--approve-large`** for a 5-point rate swing or a removed card. `publish` archives the live file, swaps atomically, and appends
to `releases/history.jsonl`; the running service hot-reloads it (an invalid file never replaces the serving snapshot). `rollback` restores a
previous release after re-validating it and checking its content hash. What it does **not** do is *find* the new rules: extracting T&Cs (scraping, PDFs,
issuer feeds) and having a person verify them is the hard part, and the `confidence` / `verified_on` fields are where that review gets recorded.

```bash
python -m scruge.rules lint data/cards.json
python -m scruge.rules diff data/cards.json new.json
python -m scruge.rules publish new.json --dry-run        # then without --dry-run; --approve-large after review
python -m scruge.rules rollback                          # back to the previous release
```

**How do you save a cap for bigger orders later?** `POST /api/plan` takes a sequence of payments and runs a branch-and-bound search that
starts from the greedy answer (so it is never worse) and prunes any branch whose best case cannot beat the incumbent (best case = each
payment's value with every cap untouched, a true upper bound). It reports `optimal` (did the search finish within its node budget) and, for the payment
happening now, whether to **hold** a capped card and why. `tests/test_planner.py` checks it against exhaustive search on 120 random cases, shows a
synthetic case (+₹150) and a real-catalog case (+₹650).

**Where do the upcoming payments come from?** From the user, not the caller. `POST /api/users/{id}/plan` forecasts the rest of the statement cycle from the
user's own ledger (`scruge/forecast.py`): it groups past payments into recurring patterns (merchant, category, channel), takes the average count per completed cycle and the
**median** amount (so one big payment does not skew it), counts what is already done this cycle so nobody is double counted, and spreads the remaining payments over the
remaining days so their order, which matters for caps, follows the person's rhythm. It then plans them against the user's stored wallet, stored cap usage and stored offer usage. The forecast and its
**confidence** (`none` / `low` = this cycle only, extrapolated and capped / `medium` = 1-2 completed cycles / `high` = 3+) come back with the plan, so the advice can be judged: on a
low-confidence forecast it is a hint, not a promise. It does not model seasonality, one-off events or income; for that you would learn from far more history than a PoC has.

**How do you detect a reward the issuer credited short?** Every committed payment stores the reward we predicted (`expected_paise`, the rule, the rules
version). `POST /api/reconcile` (or `POST /api/users/{id}/credits`) compares it with the credit on the issuer statement: within `max(₹1, 2%)` is *matched*;
less is *short*; more is *over* (our rules are stale); no credit after 45 days is *overdue*. For a mismatch it tests what the issuer probably did using the card's own
rules (paid the base rate, paid another rule's rate, paid zero, or hit a cap earlier than we assumed, blaming only the rule that was actually applied) and drafts a dispute note. The limit: it needs the issuer's
statement data (statement parsing or an account-aggregator feed); the demo in the app uses a simulated statement, labelled as such.

## API (money in paise)

| Endpoint | Purpose |
|---|---|
| `GET /api/meta` | cards (with `caps_paise` and `provenance`), merchants, MCCs, `rules_version` (ETag, gzip, 304) |
| `POST /api/optimize` | rank cards; `include_offers` adds instant offers; `?explain=false` skips the `why` trail |
| `POST /api/optimize/batch` | up to 200 payments; `sequential` applies each pick's cap use in order |
| `POST /api/commit` | apply a payment. With `user_id` + `txn_ref` it is persisted atomically and idempotent (a retry replays the stored result; the same `txn_ref` for a *different* request is a 409); without, the caller sends `cycle_usage_paise` |
| `POST /api/plan` | lookahead plan for a sequence of payments you supply (+ advice for the current one) |
| `POST /api/users/{id}/plan`, `GET /api/users/{id}/forecast` | forecast the rest of the cycle from the user's own history and plan it; nothing supplied by the caller |
| `POST /api/reconcile`, `GET/POST /api/users/{id}/reconcile`, `/credits` | expected vs credited rewards, diagnosis, dispute notes |
| `GET/PUT /api/users/{id}/state`, `/wallet`, `POST /reset` | per-user wallet, cap usage per cycle, ledger |
| `GET /api/offers` | offers with provenance |
| `GET /healthz /readyz`, `POST /api/admin/reload` | health probes and rules hot-reload |

Responses keep every field older clients read; new fields (`*_paise`, `offer`, `total_paise`) are additive. The response shape follows the request shape: send `amount_paise` and you get a lean,
integer-only response (no float fields, and cheaper to produce); send the legacy `amount` and you also get the deprecated rupee fields.

## Run and test

```bash
pip install -r requirements-dev.txt
uvicorn scruge.api:app --reload      # http://127.0.0.1:8000
python3 -m pytest -q                 # 211 tests
```

Sharing a URL (no database to host; note that wallets, usage and the ledger are lost on every restart): `USER_DB=:memory: uvicorn scruge.api:app` keeps wallets, usage and the ledger in memory, so there is nothing to provision and a restart starts everyone fresh. Run a single process (`--workers 1`), since each worker would hold its own copy. Leave `USER_DB` unset locally to keep data in `data/users.sqlite3`.

Also: `effective_from` / `effective_to` on rules (a payment carries an optional `on` date and is priced with the rules in force that day), `POST /api/compare/routes` (Checkout vs Card vs Tap vs UPI, including a plain bank account on UPI), `POST /api/compare/prices` (same product across marketplaces, ranked by price after reward and offer), `POST /api/users/{id}/statement` (CSV statement import, bad rows listed with line numbers), and an upper bound on every paise field so no integer can cause a 500.

The engine is checked against a frozen copy of its original implementation on ~16,000 random cases (`tests/test_differential.py`), so
performance work and refactors cannot silently change a recommendation. Operations are minimal (request ids, timing, an access log, health probes,
rules hot-reload); a result cache, rate limiter and metrics registry were built and then **removed** because nothing at this size justified them. A request costs about 100 µs in-process, the engine being a tenth of it.

## Known simplifications, and what I would do next

- **Rules are illustrative**, hand-written for 5 cards. Next: the extraction-and-review workflow above, with `confidence: reviewed` only after a person checks the T&Cs.
- **Offers are illustrative** and single-offer-per-card (no stacking); each carries a per-cycle limit but no expiry dates. Next: a real offer source with freshness tracking, and offers that depend on card network/BIN.
- **The forecast is simple** (recurring patterns, medians, no seasonality) and low-confidence for a new user; the planner's advice is only as good as it. Reward points are flattened to rupee value.
- **Reconciliation needs issuer statement data**; the app demonstrates it with a simulated statement. Statement dates and effective-dated rules are not modelled (cycles are calendar months).
- **SQLite** is the right size for a PoC. It runs in memory (`USER_DB=:memory:`) or in a file, with one write lock. There is **no authentication, by design for a proof of work**: a user id is a bearer secret, which is fine for a demo and not for production.
