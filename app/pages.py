"""Serves the Astro build output from dist/, plus the favicons.

Every page falls into one of four tiers, and the route order below is that
order:

* **Public** - the marketing page at /, About, the two legal pages, and
  sign-in / register / reset. Everything a person needs in order to decide
  whether to have an account, readable without one.
* **Signed in** - the Rulebook, Help, and Account. A session is all these ask
  for. A missing one redirects to /login, because a browser navigation should
  land on a sign-in page, not on JSON.
* **Members only** - the Arbiter and the Deck Builder. These still *serve* to
  any signed-in user; the page renders behind an upsell panel that app.js drops
  in when /api/auth/me reports no membership, and the endpoints behind them
  (auth.require_membership / auth.require_billing) are what actually refuse the
  work. Serving rather than redirecting is deliberate: a lapsed member sees the
  feature they're being asked to pay for, not a bounce to a billing page.
* **Admin** - signed in, plus is_admin.

Two rules keep this file honest:

1. A public page needs `public_page` on its Layout as well as a route here.
   Without it app.js bounces anonymous visitors to /login on the first 401 from
   /api/auth/me, and the page 200s to curl while being unreadable in a browser.
   (The marketing page uses HomeLayout, which loads no app.js at all.)
2. One URL per page. Each route below serves dist/<its own path>/index.html, so
   the path a page is built to is the path it is served at - no aliases, and
   nothing in dist/ that no route reaches.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import auth
from .config import DIST_DIR

router = APIRouter()


def _serve_page(rel: str) -> FileResponse:
    """FileResponse for a built page (``rel`` is a path relative to dist/).

    Resolved per request, so editing a page and re-running ``npm run build``
    shows up on the next reload with no server restart. Returns a clear 503 when
    the build is missing.
    """
    page = DIST_DIR / rel
    if page.is_file():
        return FileResponse(page)
    raise HTTPException(
        status_code=503,
        detail="Frontend build missing. Run `npm run build` to generate dist/.",
    )


def _signed_out(request: Request) -> RedirectResponse | None:
    """A redirect to /login when there's no usable session, else None.

    Written to be used as ``return _signed_out(request) or _serve_page(...)``,
    so the gate reads on the same line as the page it guards.
    """
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return None


def _dist_file(rel: str, *, cache: str | None = None) -> FileResponse:
    """Serve a single file from the dist root (e.g. a favicon). 404s when the
    build hasn't produced it, rather than the 503 used for whole pages."""
    f = DIST_DIR / rel
    if not f.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(f, headers={"Cache-Control": cache} if cache else None)


@router.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return _dist_file("favicon.ico", cache="public, max-age=86400")


@router.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return _dist_file("favicon.svg", cache="public, max-age=86400")


# ---------- public ----------

@router.get("/")
def index():
    """The marketing page - the app's front door, and the only page an
    anonymous visitor is sent to rather than bounced off. Signed-in users get
    the same page (home.js swaps the header's sign-in pair for a link into the
    app); the Arbiter itself lives at /arbiter."""
    return _serve_page("index.html")


@router.get("/about")
def about_page():
    """Public: what the service is and who made it is part of deciding whether
    to sign up, and the marketing footer links here."""
    return _serve_page("about/index.html")


@router.get("/privacy-policy")
def privacy_policy_page():
    """Public - a privacy policy has to be readable before you decide to hand
    over an email address, and payment processors and app stores expect to
    reach it without an account."""
    return _serve_page("privacy-policy/index.html")


@router.get("/terms-of-use")
def terms_of_use_page():
    """Public for the same reasons as /privacy-policy above."""
    return _serve_page("terms-of-use/index.html")


@router.get("/login")
def login_page():
    return _serve_page("login/index.html")


@router.get("/register")
def register_page():
    return _serve_page("register/index.html")


@router.get("/reset")
def reset_page():
    return _serve_page("reset/index.html")


# ---------- signed in ----------

@router.get("/rulebook")
def rulebook_page(request: Request):
    """The Comprehensive Rules, free with any account. Reading the rules is
    deliberately not behind the membership gate."""
    return _signed_out(request) or _serve_page("rulebook/index.html")


@router.get("/help")
def help_page(request: Request):
    return _signed_out(request) or _serve_page("help/index.html")


@router.get("/account")
def account_page(request: Request):
    """Billing self-service: subscription status, credit balance, checkout.
    Session-only on purpose - this is where someone without a membership goes
    to get one, so it must never be behind the membership gate."""
    return _signed_out(request) or _serve_page("account/index.html")


# ---------- members only (served signed-in; app.js draws the upsell) ----------

@router.get("/arbiter")
def arbiter_page(request: Request):
    return _signed_out(request) or _serve_page("arbiter/index.html")


@router.get("/deckbuilder")
def deckbuilder_page(request: Request):
    return _signed_out(request) or _serve_page("deckbuilder/index.html")


# ---------- admin ----------

@router.get("/admin")
def admin_page(request: Request):
    # Reads the session directly rather than going through _signed_out: this is
    # the one page that needs the row itself, not just whether there was one.
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    if not user["is_admin"]:
        # Ordinary users get bounced to the app rather than the panel.
        return RedirectResponse("/arbiter", status_code=302)
    return _serve_page("admin/index.html")
