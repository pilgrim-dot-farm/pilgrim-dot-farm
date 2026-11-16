# Payments — paid priority requests ("PAID REQUE$T")

> *Formerly `PAYMENT.md`.*

**Status: shipped** (Steps 0–12c, 2026-10-31). The single paid text box it
describes was replaced by three boxes: docs/shipped/prompt-upgrade.md.

A paid priority request on the REQUEST LINE pane, paid through Stripe Embedded
Checkout. Normative behaviour: RADIO §6.2, §9.1, §11, §12, §14; AGENTS §1
rule 11 cites §0.3 and Step 12b below.

Code: `pilgrim/payments.py` (`PaymentService`), the paid routes in
`pilgrim/server.py`, `pilgrim/web/pay.js`, `pilgrim/pipelines/copyright.py`,
`moderation.py`, `pilgrim/promotions.py` (promo codes, `make promo-codes`),
`pilgrim/tests/fakes/fake_stripe.py`, `make pay-smoke`. Stripe shapes:
docs/backends.md.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/shipped/payments.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.1 What we are building, in one paragraph

The REQUEST LINE keeps its free single-line form. Under it, a heading
**"I need about tree fiddy."** sits above a new button **PAID REQUE$T**
(background `#A6FF00`). The button opens a **multi-line** text box for a
detailed song prompt. The station moderates the text first, so nobody pays
for a request we would reject. Then Stripe's checkout form appears **inside
the pane**. The listener picks an amount (suggested $3.50, minimum $3.00,
maximum $67.69). Once Stripe confirms the payment, the request joins the
**priority queue**. Both queues are FIFO. Every paid request is produced and
aired before any free request that is still waiting.

### 0.2 How Stripe works here (for the operator and the agent)

```
 browser (pilgrim.farm)            origin (M5, server.py)               Stripe
 ───────────────────────           ──────────────────────               ──────
 1. type prompt, click
    "Check it & pay"  ── POST /api/paid-requests {text} ──▶
                                   2. prefilter + LLM moderation
                                   3. INSERT requests row
                                      status=awaiting_payment
                                   4. create Checkout Session ─────────▶ (secret key)
                                      ◀──────────── {id, client_secret}
    ◀── {token, client_secret, publishable_key} ──
 5. Stripe.js mounts Embedded
    Checkout in the pane (iframe
    served by Stripe; card data
    never touches our servers)  ─────────── card / wallet ────────────▶
                                                                       6. charge succeeds
                                   7. POST /api/stripe/webhook ◀──────── signed event
                                      verify signature, re-check
                                      amount/session/mode, then
                                      status=queued, priority=1
 8. onComplete fires; poll
    GET /api/paid-requests/{token} ──▶ status (reconciles with Stripe
    until "queued"                     if the webhook is late)
 9. board shows the paid request
    at the top of NOW QUEUED
```

Key facts:

- **Checkout Session** = a one-time payment page Stripe hosts. *Embedded*
  mode renders it as an iframe inside our page instead of redirecting to
  stripe.com. On API `2026-10-28.endive` that mode is `ui_mode=embedded_page`
  (**not** `embedded`; verified 2026-10-31 against the API reference). With
  `redirect_on_completion=never` the page stays put and Stripe.js calls our
  `onComplete` callback. `return_url` is still required for `embedded_page`
  (redirect-based payment methods return through it).
- **The price already exists** (live mode): product "Priority Song Request"
  `prod_VXVIBdZhOh1eD8`, price `price_1UWQHdKOiB5HDXLd4qNMoosI`, a
  "customer chooses" price (`custom_unit_amount`: min 300, preset 350, max
  6769 cents, USD, tax exclusive). Checkout draws the amount box itself. We
  write **no** amount input of our own.
- **The webhook is the only proof of payment.** The browser's `onComplete`
  is a hint to start polling; it never moves a request into the queue.
- **Secret key** (`sk_…` or restricted `rk_…`): server only, environment
  variable. **Publishable key** (`pk_…`): safe in the browser. **Webhook
  signing secret** (`whsec_…`): server only, used to verify webhook events.
- Test mode and live mode are separate worlds: the live price ID does not
  exist in test mode. Test mode needs its own product and price (Step 0).

### 0.3 Facts you must not get wrong

1. **Playout never waits on payment.** Nothing in `pilgrim/scheduler.py`,
   `pilgrim/selector.py` or `pilgrim/producer/` imports `pilgrim/payments.py`
   or calls Stripe. Payment only changes rows in the `requests` table; the
   producer and scheduler see a `queued` row exactly as they do today, plus a
   `priority` value used for ordering.
2. **Tests never call Stripe.** Unit, sim and e2e tests use
   `pilgrim/tests/fakes/fake_stripe.py`. Real Stripe calls happen only in
   `make pay-smoke`, and only with a **test-mode** key (Step 11).
3. **Never trust the browser about money.** Amount, currency, session ID,
   payment status and request ID come from Stripe (webhook payload or a
   server-side session retrieve), never from client input. *Operator
   exception (Step 12b):* a first line equal to the `PAID_OVERRIDE_TOKEN`
   secret comps the request (`comped=1`, amount 0, no Stripe) after the same
   checks. It is a server-side secret compare, not a client claim.
4. **Moderate before charging.** A rejected prompt never gets a Checkout
   Session. Moderation does not run again after payment. Since Step 12b the
   copied-lyrics screen also runs before Checkout, and Checkout is a separate
   call that only a row at `checking/ready` can make.
5. **No secrets in the repo, logs or responses.** Never log the secret key,
   webhook secret, `client_secret`, override token, card details or customer
   email. Return `client_secret` only once, in the response to the POST that
   created it (since Step 12b: `POST /api/paid-requests/{token}/checkout`).
6. **Additive schema only.** New columns and a new table. Never drop or
   rewrite existing request rows. Never delete `station.db`. Back it up
   before the first live start with the migration (Step 12).
7. **RADIO.md changes in the same commit** as any API, config or schema
   change (AGENTS §1 rule 8). Each step lists the sections to update.
8. **The edge and gateway allowlists must stay closed.** Add exactly the new
   paths in this plan; nothing else under `/api/` opens up. (Step 12b adds
   one: `POST /api/paid-requests/{token}/checkout`.)

### 0.4 Operator decisions already made

| Decision | Value |
|----------|-------|
| Button label | `PAID REQUE$T` |
| Button colour | `linear-gradient(90deg, #00ff66 0%, #19f5ff 50%, #00ff66 100%)` over a `#00FF66` fallback (`background-color`), dark text `#0a0118` for contrast |
| Heading above the button | `I NEED ABOUT TREE FIDDY.` (all caps), styled like the `NOW QUEUED` heading (`.req-list-head`) |
| Disclaimer under the heading | `Help us pay Amazon Web Services and Detroit Edison.` in cyan (`.pay-note`) |
| Placement | Inside the existing REQUEST LINE pane, same page, no redirect |
| Queues | Two FIFO queues: priority (paid) and free. Priority always first. |
| Paid input | Multi-line prompt for the music (free stays single-line). **Superseded by docs/shipped/prompt-upgrade.md (three boxes), 2026-11.** |
| Price | Existing Stripe price: preset $3.50, min $3.00, max $67.69, USD |
| Paid board label | Generated one-line summary of the full prompt (LLM, ≤160 chars), stored in `requests.summary`. Fallback: first non-tag line (never `[intro]`/`[verse]`). |
| Paid marker on the board | Queue number `#N` first, then the cornucopia icon (`pilgrim/web/img/cornucopia.png`) at one line high, then the request text. No `$` badge, no amount. |
| Service promise | The song is **guaranteed to air, never guaranteed when**. Paid requests jump the free line and are FIFO by payment time. Refund on request if production fails after 5 attempts, or if the station stops airing for good. |
| Request line display | Up to **20** queue rows and up to **5** "recently played" rows. Each recent row shows how long the request waited (submit → air). Both queues stay FIFO. All panes **10% wider** (440 → 484 px), not just the request line. |
| Credits pane | Heading `CREDITS` (renamed from `MODEL CREDITS`) plus an attribution row: *Cornucopia icon: Cornucopia LQ by OpenClipart, CC0* |

### 0.5 Defaults chosen by this plan (operator may override; see §14)

| Topic | Default |
|-------|---------|
| Stripe integration | Embedded Checkout (`ui_mode=embedded_page`, `redirect_on_completion=never`, no `return_url`), API version `2026-10-28.endive`; not Payment Element, not Payment Links |
| Managed Payments | **Off** per session (`managed_payments[enabled]=false`): the product tax code is ineligible and Pilgrim Dot Farm LLC stays merchant of record. Turning it on is an operator decision (needs an eligible tax code). Recommended: also switch the account default off in Dashboard → Settings → Managed Payments, so no future product silently becomes merchant-of-record. |
| HTTP to Stripe | `httpx.AsyncClient` (form-encoded), webhook check with stdlib `hmac`. **No `stripe` Python package.** This matches AGENTS §3 (httpx, explicit timeouts) and keeps the fake simple. |
| Paid prompt limit | 3600 characters, 72 lines. **Superseded by docs/shipped/prompt-upgrade.md (three boxes: 8000 composed, 4000/3000/800 per box), 2026-11.** |
| Paid text path | Goes through the song **brief** LLM like a free request (style lock, validation, QC all apply), with a stronger "honour the listener's details" instruction. It is **not** sent raw to MiniMax. **Superseded by docs/shipped/prompt-upgrade.md (lyrics and music boxes reach the brief labelled; art goes to the scene writer), 2026-11.** |
| Paid song length | Never shortened by the "≥ 3 queued → short song" backlog rule |
| Production attempts | Paid: 5, free: 3 (unchanged) |
| Failed paid request | Marked `failed`, logged as `payment.refund_due`, listed on an admin route. **No automatic refund.** The operator refunds in the Stripe Dashboard. |
| Amount shown publicly | Never. The board shows the cornucopia icon only. |
| Checkout expiry | 30 minutes (Stripe's minimum) |
| Stripe.js loading | Lazy: loaded on the first PAID REQUE$T click, not on page load |

### 0.6 Ports, paths and names introduced

| Thing | Value |
|-------|-------|
| New module | `pilgrim/payments.py` |
| New fake | `pilgrim/tests/fakes/fake_stripe.py` |
| New client script | `pilgrim/web/pay.js` |
| New prompts | `pilgrim/prompts/paid_request.md`, `pilgrim/prompts/paid_summary.md` |
| New public routes | `POST /api/paid-requests`, `GET /api/paid-requests/{token}`, `POST /api/stripe/webhook` |
| New admin route | `GET /api/admin/payments` |
| New env vars | `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` |
| New Make target | `make pay-smoke` |
| Webhook URL (live) | `https://pilgrim.farm/api/stripe/webhook` |
| Live webhook endpoint | `we_1UWU1pKOiB5HDXLdhsCpnggq` (created 2026-10-31, `enabled`, `api_version: null` = account default) |
| New asset | `pilgrim/web/img/cornucopia.png` — **Cornucopia LQ** by OpenClipart, **CC0 1.0**, via Wikimedia Commons; cropped to its alpha box and downscaled to 160×125 PNG-8 (7.6 KB, transparent). The original `thanksgiving-png-44560.png` is *personal use only* and is not used. |
| Asset attribution | "Cornucopia LQ" by OpenClipart, CC0 1.0, <https://commons.wikimedia.org/wiki/File:Cornucopia_LQ.svg> — credited in the CREDITS pane, RADIO §9.1 and `docs/models.md` (CC0 does not require it; we credit anyway) |

## Step 12b. Staged checks, copied-lyrics screen, operator comp (Oct 31)

Operator decisions: show three honest check stages; screen paid lyrics for
copying with a quick web search; a degraded search lets the request proceed;
one famous line is allowed; poll rather than stream; an operator comp secret
for testing the priority queue without real transactions or Stripe test mode.

**Flow.** `POST /api/paid-requests` does rate limit, normalise, the comp
check and the prefilter, stores a `checking` row (stage `moderation`) and
returns `{ok, token, stage}` at once ("Checking with the DJ…"). A background
task then runs moderation ("Checking for unseemly content…"), the copyright
screen ("Checking for existing copyrighted material…") and the board
summary, and sets stage `ready`. The client polls `GET
/api/paid-requests/{token}` every second and, at `ready`, calls `POST
/api/paid-requests/{token}/checkout`, which claims the row once
(`-> awaiting_payment`) and creates the Checkout Session. `client_secret`
is returned there once and never stored. A crash in the checks rejects the
row ("You were not charged"); `checking` rows are abandoned on restart.

**Copyright screen** (`pilgrim/pipelines/copyright.py`, config
`payments.copyright`). Up to 4 distinctive lyric lines (lines under section
tags, or quoted passages; at least 6 words, 3 of them not stopwords) are
searched as `"<line>" lyrics` through searxng, pinned to `startpage`
(docs/backends.md §5). A hit is 6+ consecutive matching words between the
line and a result snippet. **Reject** when two probed lines hit the same song
(result titles, site names dropped) or when a hit's snippet continues with
words that also appear in the submission (a copied passage). One isolated
hit is allowed. All probes degraded or timed out → `unchecked`, logged as
`payment.copyright_unchecked`, request proceeds. No LLM sees the snippets or
makes the decision. The outcome is "no match found", never "copyright-free".
`paid_request.md` also tells the brief not to reproduce existing lyrics.

**Operator comp.** `PAID_OVERRIDE_TOKEN` (env / origin `.env`, ≥ 24
chars, else off). First line equal to it (constant-time compare) → stripped
before anything stores, logs, moderates or shows the text; moderation and
the screen still run; then `queued`, `comped=1`, `amount_cents=0`,
`paid_at` set, no session. Works even with payments inactive (the public
button still shows closed then; the API accepts the comp). The secret
anywhere else in a paid or free request → rejected, not stored. Logged as
`payment.comped` (request id only).

**Schema/API/config:** `requests.stage`, `requests.comped`, status
`checking`; the routes above (RADIO.md §11); `payments.copyright` (§12).
Edge and gateway gain the checkout location. `make pay-smoke` is not part of
this step (operator does not use Stripe test mode); the fake covers the
real-payment branch.

---

## Step 12c. Payer's MiniMax input used as written; farm lock (Oct 31)

Operator: the closer a submission is to MiniMax input, the less work the
station does on it; the LLM interprets arbitrary English; limit 6,000
characters (prompt and audio token budgets are separate, so a full caption +
lyric sheet, ~1,500 tokens, always fits the 4,096 prompt context).

- `payments.paid_max_length: 6000`, `paid_max_lines: 200`, textarea
  `maxlength="6000"`.
- `pipelines/verbatim.py` finds a structured caption (three headings in
  order) and tagged lyrics in the request. The brief LLM decides from the
  listener's words whether to use them as written (`caption_verbatim`,
  `lyrics_verbatim`; default yes for a complete block unless asked to rework)
  and the code substitutes the exact blocks. Tags only are normalised (own
  line, lowercase, synonyms like `[Hook]` -> `[chorus]`, unknown tag lines
  dropped, words kept).
- The copyright screen removes a pasted caption before choosing probes.
- Creative direction (RADIO §6.2): stock songs and free requests deliberately
  receive the Pilgrim Dot Farm treatment in their title, artist and lyrics,
  while keeping the requested musical style. Natural station references and
  full farm stories are welcome; station-name title tags are rejected on this
  path. Paid requests, including operator comps, follow the customer's brief
  without unsolicited farm humor or station branding in any field. Requested
  titles are preserved, including parenthetical text. The shared musical
  prompt plus exactly one creative direction are sent as the system message,
  so paid and free instructions do not compete across message priorities.
- Verbatim preservation is paid-only. Free requests containing tagged lyrics
  or captions are adapted to the station treatment even if the brief LLM
  returns verbatim flags. The payer's own lyrics remain exempt from style
  rules; generated parts retain the musical style checks.

## 14. Open questions for the operator

Defaults are in §0.5. The agent builds the default unless told otherwise.

1. **Refund policy / terms text** for the site (Stripe requirement). Who
   writes it, and where does it go (footer link? a line in the paid panel)?
   **Answered 2026-10-31:** the service promise is *guaranteed to air, no
   guarantee when* (product description already says timing depends on queue
   length). Proposed copy for the paid panel: *"Your request will air — we
   don't promise when. It jumps the free line. If we can't make your song,
   you get your money back."* Proposed refund line: *"Paid requests are
   produced and aired ahead of free requests, first-come first-served. We
   guarantee the song will air, not a time. If we fail to produce it after 5
   attempts, or the station goes off the air for good, we refund on
   request."* Placement still needs the operator's call (footer link vs. a
   line in the paid panel).
2. **Refunds for failed paid requests**: manual in the Dashboard (default),
   or should the station call the Refunds API itself after the 5th failed
   attempt? Automatic refunds need Refunds: Write on the station key.
   **Answered 2026-10-31: manual in the Dashboard. No automatic refund.**
3. **Refunded or disputed before airing**: keep the request in the queue
   (default) or cancel it?
   **Answered 2026-10-31: keep it queued — we expect the station to air it.**
4. **Paid prompt freedom**: through the brief LLM (default, safer), or pass
   the listener's lyric lines verbatim to MiniMax?
   **Answered 2026-10-31: through the brief LLM. The prompt tells the brief
   LLM explicitly that this request was paid (versus a free one-line wish)
   and requires stronger adherence to the listener's named details.**
5. **Free-line phone filter**: the "count all digits" rule also rejects
   harmless free requests such as "a 1970s song at 120 bpm". Switch the free
   line to `"runs"` too? (Default: no; it is a separate change.)
6. **Showing the paid text on the board**: first line only, cut to 160
   characters (default), or a fixed label such as "Priority request"?
   **Answered 2026-10-31: first line, cut to 160, with the cornucopia icon
   as the leading character (§0.4).**
7. **Stripe Tax**: the price is tax exclusive and no automatic tax is
   configured. Leave it off (default) or enable Stripe Tax?
8. **Receipts**: Stripe can email receipts (Checkout collects the email).
   Turn on receipt emails in Dashboard settings? (No code change either way;
   the station never stores the email.)
