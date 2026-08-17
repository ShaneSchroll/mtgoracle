"""server.py - the ASGI entrypoint for the Arbiters Grimoire.

The application itself lives in the `app/` package; this module exists so that
`uvicorn server:app` keeps working. That command is written into README.md, the
operator manual in documentation/, and the start command in the Render
dashboard - which lives outside this repo and cannot be updated atomically with
a deploy - so the import path stays put even though the code moved.

Where things went:
    app/config.py        paths, model config, chat caps (and load_dotenv)
    app/main.py          the FastAPI instance, middleware, mounts, routers
    app/security.py      CSP + security headers, the static-file mounts
    app/pages.py         serves the Astro build out of dist/
    app/rules.py         BM25 retriever, subrule index, CR version
    app/chat.py          /api/chat - retrieval, the Claude tool loop, SSE
    app/cards.py         card text lookup + autocomplete
    app/conversations.py saved chat history
    app/admin_api.py     /api/admin
    app/auth/            everything that touches users.db

Flow for each user question:
  1. Retrieve the most relevant rulebook chunks (BM25 over your TXT).
  2. Send them to Claude alongside an MTG-expert system prompt.
  3. If Claude asks to look up a card, call Scryfall and feed the result back.
  4. Return Claude's final answer plus the rule sources used.

Auth:
  The chat endpoint requires a signed-in, non-suspended user. Registration is
  open (accounts arrive approved); app/auth/ + admin.py own account creation and
  the revoke/reinstate switch. See admin.py for bootstrap.

Run:  uvicorn server:app --port 8000
Behind a proxy (Render):
      uvicorn server:app --host 0.0.0.0 --port 8000 \\
              --proxy-headers --forwarded-allow-ips '*'
  so that request.url.scheme reports https and cookies get the Secure flag.
"""

from app.main import app  # noqa: F401  - re-exported for `uvicorn server:app`
