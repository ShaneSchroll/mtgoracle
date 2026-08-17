# MTG Arbiters Grimoire

A web-based chat assistant that answers Magic: The Gathering rules questions,
grounded in the official rulebook PDF and backed by Claude. It retrieves the
relevant rules for every question, cites rule numbers, and can look up live
card text from Scryfall when a question names a specific card.

## What's inside

| File                | Role                                                        |
|---------------------|-------------------------------------------------------------|
| `ingest.py`         | Turns the official rules `.txt` into searchable `rules.json` + `docs.json`. |
| `retriever.py`      | BM25 keyword search over those chunks.                      |
| `mtg_api.py`        | Scryfall card-lookup tool exposed to Claude.                |
| `server.py`         | ASGI entrypoint — re-exports `app` so `uvicorn server:app` works. |
| `app/`              | The FastAPI backend (see below).                            |
| `admin.py`          | CLI for managing users and minting password-reset links.   |
| `src/`              | Astro frontend source: pages, layouts, components, scripts, styles. |
| `scss/`             | SCSS partials compiled to `src/styles/styles.css` (Prepros, on save). |
| `public/fonts/`     | Self-hosted Inter + EB Garamond woff2 and their OFL licence.  |
| `dist/`             | Astro build output (the HTML/JS/CSS the server actually serves.) |

Inside `app/`:

| Module              | Role                                                        |
|---------------------|-------------------------------------------------------------|
| `main.py`           | Builds the FastAPI app: middleware, static mounts, routers. |
| `config.py`         | Paths, model config, chat caps. Calls `load_dotenv()` first. |
| `security.py`       | CSP + security headers, the two static-file mounts.         |
| `pages.py`          | Serves the built Astro pages out of `dist/`.                |
| `rules.py`          | BM25 retriever, per-subrule index, CR effective date.       |
| `chat.py`           | `/api/chat` — retrieval, the Claude tool loop, SSE.         |
| `cards.py`          | Card text lookup and decklist autocomplete.                 |
| `conversations.py`  | Saved chat history for the sidebar archive.                 |
| `deckbuilder.py`    | AI Deck Builder: legality rules, deck analysis, saved decks. |
| `billing.py`        | Stripe checkout, portal, and the signature-verified webhook. |
| `admin_api.py`      | `/api/admin` — the web mirror of every `admin.py` command.  |
| `auth/`             | Everything that touches `users.db` — accounts, sessions, credits. |

## Setup

```bash
# 1. Install backend dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. Add your ANTHROPIC_API_KEY to a .env file

# 3. Build the rules index from the official .txt (re-run when the rules update)
#    Download it from https://magic.wizards.com/en/rules as rules.txt
python ingest.py rules.txt

# 3a. Build the cards index from Scryfall's Oracle Cards bulk export (gzipped)
#     https://scryfall.com/docs/api/bulk-data — same data build_card_cache.py pulls live
python card_ingest.py oracle-card-data.jsonl.gz

# 3b. Locally, pass --out if CARD_DB_PATH lives only in .env (the CLI doesn't
#     call load_dotenv). On Render it's a real env var, so --out isn't needed.
python card_ingest.py oracle-card-data.jsonl.gz --out /var/data/cards.db

# 4. Build the frontend (Node 22.12+). Re-run after editing anything in src/.
npm install
npm run build

# 5. Start the app — it serves the built site out of dist/
uvicorn server:app --port 8000
```

## Importing a collection into the Deck Builder

The Deck Builder's **Import CSV** button reads the exports that collection
trackers produce — ManaBox, Moxfield, Archidekt, Deckbox. Columns are sniffed by
name rather than hardcoded, so an app that calls its quantity column `Count`
works the same as one that calls it `Quantity`.

Everything except the card name and the quantity is discarded, including the
Scryfall ID. `cards.db` is built from Scryfall's *Oracle Cards* export, which
keeps one representative printing per gameplay-unique card, while a tracker
exports the exact printing you own — on a real ManaBox export, 29% of rows named
a different printing than the cached one. Names matched 100% of the same file,
and every field the builder reads (mana cost, type line, legality, ban list) is
identical across printings.

Duplicate printings of one card merge into a single line with a summed count, so
two `Rite of Replication` rows become `2 Rite of Replication`. Names the local
cache doesn't recognise are kept in the list and reported, not silently dropped.

A deck holds `MAX_DECK_CARDS` (150) distinct cards; a larger file is refused with
a count and a note to trim it. The uploaded CSV is parsed in memory and
discarded — it is never written to disk or stored in the database, and importing
does not save a deck. You review the list, name it, and save as usual.

## Who can see what

| Tier          | Pages                                                                   |
|---------------|-------------------------------------------------------------------------|
| Public        | `/` (marketing), `/about`, `/privacy-policy`, `/terms-of-use`, `/login`, `/register`, `/reset` |
| Signed in     | `/rulebook`, `/help`, `/account`, and every `/api/*` that reads your own data |
| Members       | `/arbiter`, `/deckbuilder`                                              |
| Admins        | `/admin`                                                                |

Public pages take two changes, not one: a route with no session check in
`app/pages.py` **and** `public_page` on the page's Layout. Without the second,
`app.js` bounces anonymous visitors to `/login` on the first 401 from
`/api/auth/me` — the route returns 200 to `curl` while being unreadable in a
browser. (`/` is exempt: it uses `HomeLayout`, which loads no `app.js`.)

A membership is a live subscription (`active`, `trialing`, or an admin `comp`),
and it gates only the Arbiter and the Deck Builder. Everything else — the
Rulebook, card lookups, and the rulings and decks you already saved — needs
nothing but a session, so a lapsed membership never locks you out of your own
work.

Both members-only pages are still *served* to any signed-in user: they render
behind a panel explaining what a membership buys, and the endpoints behind them
(`auth.require_membership`, `auth.require_billing`) are what actually refuse the
work with a 402. Anonymous visitors are redirected to `/login` instead, and the
whole gate is inert until `BILLING_REQUIRED=1`.

## Billing ($5/mo subscription + prepaid credits)

Every new account starts on a 7-day trial. After that, users pay a $5/month
Stripe subscription for access plus prepaid usage credits ($5 / $10 / $20
packs) that are deducted as they use the Arbiter and Deck Builder.

## The admin panel

Every command below also has a button, at **/admin** — visible only to accounts
with `is_admin`. The panel adds a users table showing every field stored on an
account, and it can rebuild the rules index and the card cache from uploaded
files without touching a terminal.

Its command forms and its shell-command reference are generated by introspecting
the real `argparse` parsers, so they cannot drift from the CLI: add a flag to
`admin.py` and it appears in the panel on the next reload.

Long rebuilds record their progress in the `admin_jobs` table rather than in
process memory, so the status poll works no matter which uvicorn worker answers
it, and closing the page doesn't lose the job. Both rebuilds still need a server
restart to take effect — the retriever and the card cache are built once at
import — and the panel shows a banner when disk and memory disagree.

Bootstrap the first admin from the CLI: `python admin.py create you@example.com
--admin --approved`.

## Commands to work with users

```bash
python admin.py list
python admin.py create  alice@example.com [--admin] [--approved]
# Registration is open — accounts arrive usable. `revoke` suspends one,
# `approve` puts it back; `--all` reinstates everyone currently suspended.
python admin.py revoke  alice@example.com
python admin.py approve alice@example.com
python admin.py approve --all
python admin.py make-admin alice@example.com
python admin.py reset   alice@example.com --base-url https://arbitersgrimoire.com
python admin.py delete  alice@example.com --yes

# Monthly spend limits are in credit dollars; users set their own on /account.
python admin.py budget alice@example.com --usd 2.50    # override their limit
python admin.py budget alice@example.com --unlimited   # no cap
python admin.py budget alice@example.com --default     # back to global default
python admin.py usage  alice@example.com               # this month's spend + remaining

# Users self-serve their display name and password from the Account page
# (/account) — the reset command below is for when they're locked out.

# Grants bypass the $20 purchase cap — they're the owner override. A negative
# amount claws back, e.g. to mirror a chargeback settled in Stripe.
python admin.py credits alice@example.com 5            # grant $5 of usage credits
python admin.py credits alice@example.com -2.50        # claw back
python admin.py comp    alice@example.com on           # complimentary subscription
```

Then open **http://localhost:8000**.

The ingester is tuned for the official Comprehensive Rules (chunks by rule
number like `509.2`). If you feed it a prose-style rulebook instead, it
automatically falls back to fixed-size chunks.

## How it works

For each question the backend
    1. Retrieves the most relevant rulebook chunks (official rulebook parsed into JSON for the AI)
    2. Sends them to Claude with a judge-level system prompt and the users board state / question
    3. Lets Claude call the Scryfall tool if a card is named and not cached in cards.db
    4. Returns the answer plus the rule sources it cites, shown as chips under each reply
