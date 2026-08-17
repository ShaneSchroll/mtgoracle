"""Builds the FastAPI application and wires every router onto it.

Nothing else in the package imports this module - routers are defined in their
own modules and included here - so the import graph stays acyclic.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from . import (
    admin_api, auth, billing, cards, chat, conversations, deckbuilder, pages,
    rules,
)
from .config import (
    ALLOWED_MODELS, ASTRO_ASSETS, DEFAULT_MODEL, DIST_DIR, MODEL_CALL_PARAMS,
)
from .llm import client
from .security import (
    FontStaticFiles, ImmutableStaticFiles, SecurityHeadersMiddleware,
)

deckbuilder.configure(client, ALLOWED_MODELS, DEFAULT_MODEL, MODEL_CALL_PARAMS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.init_db()
    yield

# docs_url/redoc_url/openapi_url disabled: the interactive docs would hand
# anonymous visitors the full API map, including every admin route.
app = FastAPI(
    title="Arbiters Grimoire",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth.router)

# Astro's hashed JS/CSS bundles. check_dir=False so the app still boots when
# dist/ hasn't been built yet (the page routes return a clear 503 in that case).
app.mount(
    "/_astro",
    ImmutableStaticFiles(directory=ASTRO_ASSETS, check_dir=False),
    name="astro",
)

# Self-hosted Inter (public/fonts -> dist/fonts), replacing the rsms.me CDN so
# no third party ever sees a visitor's IP. Public by design: the sign-in page
# needs the typeface before anyone has a session, and the files are freely
# redistributable under the OFL (public/fonts/LICENSE.txt ships with them).
app.mount(
    "/fonts",
    FontStaticFiles(directory=DIST_DIR / "fonts", check_dir=False),
    name="fonts",
)


# Routers, in the order their routes were registered before the split. Every
# path is a distinct literal, so the order is documentation rather than
# behaviour - but keeping it makes the before/after route table easy to compare.
app.include_router(pages.router)
app.include_router(rules.router)
app.include_router(chat.router)
app.include_router(cards.router)
app.include_router(conversations.router)
app.include_router(admin_api.admin_router)
app.include_router(deckbuilder.router)

# Stripe billing: checkout/portal/summary under /api/billing plus the
# signature-verified webhook at /api/stripe/webhook. Safe to include when
# Stripe env vars are absent - endpoints then report "not configured".
app.include_router(billing.router)
app.include_router(billing.webhook_router)
