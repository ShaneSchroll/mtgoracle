"""
billing.py - Stripe subscription + prepaid usage credits.

Money model (see SETUP-BILLING.md for the dashboard/Render/Cloudflare setup):
  * $5/month subscription  -> access to the app (Stripe Checkout, mode
    "subscription"; the price lives on a dashboard Price whose id is
    STRIPE_PRICE_SUBSCRIPTION).
  * Prepaid credit packs   -> fuel for actual AI usage (Checkout, mode
    "payment", inline price_data - no dashboard product needed). A paid pack
    credits its face value in micro-dollars; auth.record_usage deducts the
    marked-up cost of every request. Credits are NON-REFUNDABLE and capped at
    auth.MAX_CREDIT_BALANCE_USD per account, so nobody can buy more than they
    could plausibly want to lose.

The webhook is the source of truth: purchases are credited and subscription
state mirrored ONLY from signature-verified Stripe events (or from
/api/billing/refresh, which asks Stripe directly). Nothing is ever credited
from the browser's success redirect. Crediting is idempotent via the UNIQUE
stripe_ref column - Stripe retries deliveries and refresh re-lists old
sessions, and both paths collapse into "already credited".

Env:
  STRIPE_SECRET_KEY          sk_test_... / sk_live_...   (unset = billing off)
  STRIPE_WEBHOOK_SECRET      whsec_... for /api/stripe/webhook
  STRIPE_PRICE_SUBSCRIPTION  price_... of the $5/mo recurring Price
  APP_BASE_URL               https://arbitersgrimoire.com (checkout redirects)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import auth

try:  # The server must still boot in a dev env without the stripe package.
    import stripe
except ImportError:  # pragma: no cover
    stripe = None

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_SUBSCRIPTION = os.getenv("STRIPE_PRICE_SUBSCRIPTION", "")

if stripe is not None and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Display only; the charged amount lives on the Stripe Price object.
SUBSCRIPTION_LABEL = "$5 / month"

# The one-time credit packs offered, in cents ($5 / $10 / $20). Server-side
# allowlist so the client can't invent amounts; a paid pack credits
# cents * 10_000 micro-dollars (face value - the margin is taken by
# auth.CREDIT_MARKUP at deduction time). The largest pack matches the balance
# cap, so an empty account can top all the way up in one purchase.
CREDIT_PACKS_CENTS = (500, 1000, 2000)

# Checkout/portal/refresh all call out to Stripe; keep one user from hammering
# those round-trips. Session creation is cheap but not free.
_billing_limit = auth._RateLimiter("billing", 10, timedelta(minutes=1))

router = APIRouter(prefix="/api/billing", tags=["billing"])
webhook_router = APIRouter(tags=["billing"])


def configured() -> bool:
    return stripe is not None and bool(STRIPE_SECRET_KEY)


def _require_configured() -> None:
    if not configured():
        raise HTTPException(
            status_code=503,
            detail="Billing is not configured yet (STRIPE_SECRET_KEY is unset).",
        )


def _rate_limit(user_id: int) -> None:
    if _billing_limit.hit(str(user_id)):
        raise HTTPException(429, "Too many billing requests. Please wait a moment.")


def _base_url(request: Request) -> str:
    """Absolute origin for Checkout redirect URLs. Shared with the reset-link
    routes: APP_BASE_URL in production (set it - Stripe rejects relative
    URLs), derived from the request in dev."""
    return auth.base_url(request)


def _ensure_customer(user_row) -> str:
    """This user's Stripe Customer id, creating the Customer on first use.
    Metadata carries our user id so a human reading the Stripe dashboard can
    map a customer back to an account."""
    row = auth.get_user_by_id(user_row["id"])
    if row["stripe_customer_id"]:
        return row["stripe_customer_id"]
    customer = stripe.Customer.create(
        email=row["email"],
        name=row["name"] or None,
        metadata={"user_id": str(row["id"])},
    )
    auth.set_stripe_customer(row["id"], customer["id"])
    # Re-read: if a concurrent request won the first-writer-wins update, use
    # the stored id so both requests continue with the same customer.
    return auth.get_user_by_id(row["id"])["stripe_customer_id"]


def _period_end_iso(sub) -> Optional[str]:
    """Paid-through timestamp of a Subscription as ISO-8601 UTC. Newer Stripe
    API versions moved current_period_end from the subscription onto its
    items, so check both shapes."""
    ts = sub.get("current_period_end")
    if not ts:
        items = ((sub.get("items") or {}).get("data")) or []
        if items:
            ts = items[0].get("current_period_end")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _apply_subscription(user_id: int, sub, *, deleted: bool = False) -> None:
    """Mirror one Stripe Subscription onto the user row, including whether it
    is winding down. Cancellations made in the Stripe Customer Portal arrive
    here as `cancel_at_period_end`, so the portal and our own Cancel button
    converge on the same local state."""
    status = "canceled" if deleted else (sub.get("status") or "canceled")
    auth.set_subscription(user_id, status, _period_end_iso(sub))
    if deleted:
        auth.end_access_now(user_id)
    elif sub.get("cancel_at_period_end"):
        auth.cancel_subscription(user_id)  # no-op if already cancelled
    elif status in ("active", "trialing"):
        # A live, non-cancelling subscription - drop any stale cancel state so
        # resubscribing after a lapse actually restores access.
        auth.clear_cancellation(user_id)


def _current_subscription(customer_id: str):
    """The subscription we'd act on for this customer, if any. Prefers a live
    one; ignores fully-ended subscriptions, which can't be modified."""
    if not customer_id:
        return None
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
    for sub in subs.get("data") or []:
        if sub.get("status") in ("active", "trialing", "past_due"):
            return sub
    return None


def _credit_paid_session(user_id: int, session) -> None:
    """Credit a paid one-time Checkout Session at face value, keyed by the
    session id so retries/re-lists can't double-credit."""
    cents = int(session.get("amount_total") or 0)
    if cents <= 0:
        return
    auth.add_credits(
        user_id,
        cents * 10_000,  # cents -> micro-dollars
        "purchase",
        stripe_ref=session.get("id"),
        note=f"Credit pack ${cents / 100:.2f}",
    )


def _user_id_from(obj) -> Optional[int]:
    """Resolve which of our users a Stripe object belongs to: the metadata we
    stamp at checkout first, then client_reference_id, then the stored
    customer mapping."""
    meta = obj.get("metadata") or {}
    for raw in (meta.get("user_id"), obj.get("client_reference_id")):
        if raw:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    row = auth.get_user_by_stripe_customer(obj.get("customer") or "")
    return row["id"] if row else None


# ---------- authenticated billing endpoints ----------

class CheckoutReq(BaseModel):
    kind: Literal["subscription", "pack"]
    pack_cents: Optional[int] = None


@router.post("/checkout")
def create_checkout(req: CheckoutReq, request: Request, user=Depends(auth.require_user)):
    """Create a hosted Stripe Checkout session and hand its URL back for the
    browser to navigate to. Card data never touches this server."""
    auth.require_same_origin(request)
    _require_configured()
    _rate_limit(user["id"])

    # Everything we can refuse from local state is refused BEFORE touching
    # Stripe, so a purchase we were never going to allow doesn't mint a
    # Customer as a side effect.
    if req.kind == "pack":
        if req.pack_cents not in CREDIT_PACKS_CENTS:
            raise HTTPException(400, "Unknown credit pack.")
        # Credits are only sellable to a live, non-cancelling subscription
        # holding less than the cap - otherwise someone winding down could buy
        # fuel they can't burn, or stack up non-refundable credits.
        blocked = auth.purchase_blocked(auth.get_user_by_id(user["id"]))
        if blocked:
            raise HTTPException(status_code=402, detail=blocked)
        # The cap again, per pack: purchase_blocked only refuses an account
        # already AT the cap, this refuses the pack that would push it over.
        # The account page disables those buttons; this is the real guard.
        if not auth.pack_affordable(user["id"], req.pack_cents):
            headroom = auth.credit_headroom_micros(user["id"]) / 1_000_000
            raise HTTPException(status_code=402, detail={
                "code": "credit_limit_reached",
                "message": f"That pack would put you over the "
                           f"${auth.MAX_CREDIT_BALANCE_USD:.0f} credit limit — "
                           f"you can add up to ${headroom:.2f} right now.",
            })
    elif not STRIPE_PRICE_SUBSCRIPTION:
        raise HTTPException(
            503, "Subscription price is not configured (STRIPE_PRICE_SUBSCRIPTION)."
        )

    customer_id = _ensure_customer(user)
    base = _base_url(request)
    common = {
        "customer": customer_id,
        "success_url": f"{base}/account?checkout=success",
        "cancel_url": f"{base}/account?checkout=canceled",
        "client_reference_id": str(user["id"]),
    }

    if req.kind == "subscription":
        # Only a real Stripe subscription blocks a new checkout. A local
        # signup trial also carries status 'trialing', and converting it into
        # a paid subscription before it lapses is exactly the point.
        if _current_subscription(customer_id) is not None:
            raise HTTPException(
                409,
                "You already have a subscription - use Manage subscription "
                "(or Resume, if you've cancelled).",
            )
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_SUBSCRIPTION, "quantity": 1}],
            metadata={"user_id": str(user["id"])},
            subscription_data={"metadata": {"user_id": str(user["id"])}},
            **common,
        )
    else:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": req.pack_cents,
                    "product_data": {
                        "name": f"Arbiters Grimoire usage credits "
                                f"(${req.pack_cents / 100:.0f})",
                    },
                },
                "quantity": 1,
            }],
            metadata={"user_id": str(user["id"])},
            **common,
        )
    return {"url": session["url"]}


@router.post("/portal")
def create_portal(request: Request, user=Depends(auth.require_user)):
    """Stripe Customer Portal - cancel/renew the subscription, update the
    card, see invoices. Requires the portal to be activated in the dashboard."""
    auth.require_same_origin(request)
    _require_configured()
    _rate_limit(user["id"])
    row = auth.get_user_by_id(user["id"])
    if not row["stripe_customer_id"]:
        raise HTTPException(400, "No billing profile yet - subscribe or buy credits first.")
    session = stripe.billing_portal.Session.create(
        customer=row["stripe_customer_id"],
        return_url=f"{_base_url(request)}/account",
    )
    return {"url": session["url"]}


@router.post("/cancel")
def cancel_subscription(request: Request, user=Depends(auth.require_user)):
    """Cancel at the end of what the user already has - the rest of the trial,
    or the period their card has already paid for. Nothing is billed again and
    nothing is cut short: credits are non-refundable, so the one thing we owe
    someone on the way out is the chance to spend the balance they bought.
    """
    auth.require_same_origin(request)
    _rate_limit(user["id"])
    row = auth.get_user_by_id(user["id"])
    if auth.is_canceled(row):
        raise HTTPException(409, "Your subscription is already cancelled.")
    if row["subscription_status"] == "comp":
        raise HTTPException(400, "Complimentary access has no subscription to cancel.")
    if not auth.subscription_ok(row):
        raise HTTPException(400, "You don't have an active subscription or trial.")

    # Stripe first: if it fails we must not lock the user out locally while
    # their card keeps being charged.
    if configured() and row["stripe_customer_id"]:
        sub = _current_subscription(row["stripe_customer_id"])
        if sub:
            stripe.Subscription.modify(sub["id"], cancel_at_period_end=True)

    result = auth.cancel_subscription(row["id"])
    if result is None:  # lost a race with a concurrent cancel
        raise HTTPException(409, "Your subscription is already cancelled.")
    return {"ok": True, **result}


@router.post("/resume")
def resume_subscription(request: Request, user=Depends(auth.require_user)):
    """Undo a cancellation that hasn't taken effect yet, re-enabling credit
    purchases. Once access has actually lapsed there is nothing to resume and
    the user needs a fresh subscription."""
    auth.require_same_origin(request)
    _rate_limit(user["id"])
    row = auth.get_user_by_id(user["id"])
    if not auth.is_canceled(row):
        raise HTTPException(409, "Your subscription isn't cancelled.")

    if configured() and row["stripe_customer_id"]:
        sub = _current_subscription(row["stripe_customer_id"])
        if sub and sub.get("cancel_at_period_end"):
            stripe.Subscription.modify(sub["id"], cancel_at_period_end=False)

    if not auth.resume_subscription(row["id"]):
        raise HTTPException(
            409,
            "Your access has already ended - subscribe again to keep using the Arbiter.",
        )
    return {"ok": True}


class MonthlyLimitReq(BaseModel):
    usd: float


@router.post("/monthly-limit")
def set_monthly_limit(req: MonthlyLimitReq, request: Request, user=Depends(auth.require_user)):
    """Let the user cap what they can burn through in a calendar month. Their
    first purchase seeds this with their whole balance
    (auth.ensure_monthly_limit_default); this is how they tighten it, and
    topping up later never moves it back."""
    auth.require_same_origin(request)
    _rate_limit(user["id"])
    lo = auth.MIN_MONTHLY_LIMIT_MICROS / 1_000_000
    hi = auth.MAX_MONTHLY_LIMIT_MICROS / 1_000_000
    if not (lo <= req.usd <= hi):
        raise HTTPException(
            400, f"Monthly limit must be between ${lo:.2f} and ${hi:.0f}."
        )
    auth.set_user_monthly_limit(user["id"], int(round(req.usd * 1_000_000)))
    return {
        "ok": True,
        "monthly_limit": auth.monthly_limit_view(auth.get_user_by_id(user["id"])),
    }


@router.get("/summary")
def billing_summary(user=Depends(auth.require_user)):
    """Everything the account page renders in one call."""
    row = auth.get_user_by_id(user["id"])
    purchase_block = auth.purchase_blocked(row)
    headroom = auth.credit_headroom_micros(row["id"])
    return {
        "configured": configured(),
        "billing_required": auth.BILLING_REQUIRED,
        "has_customer": bool(row["stripe_customer_id"]),
        "subscription": {
            "status": row["subscription_status"],
            "ok": auth.subscription_ok(row),
            "period_end": row["subscription_period_end"],
            "price_label": SUBSCRIPTION_LABEL,
            "in_trial": auth.in_trial(row),
            "trial_ends_at": row["trial_ends_at"],
            "trial_days": auth.TRIAL_DAYS,
            "canceled": auth.is_canceled(row),
            "canceled_at": row["canceled_at"],
            "access_ends_at": row["access_ends_at"],
            "can_resume": auth.can_resume(row),
        },
        "credits": {
            "balance_usd": round(
                auth.credit_balance_micros(row["id"]) / 1_000_000, 2
            ),
            # Each pack carries whether it fits under the cap, so the page can
            # grey out just the ones that would overshoot instead of the row.
            "packs": [
                {"cents": c, "affordable": c * 10_000 <= headroom}
                for c in CREDIT_PACKS_CENTS
            ],
            "can_purchase": purchase_block is None,
            "blocked_reason": purchase_block["message"] if purchase_block else None,
            "blocked_code": purchase_block["code"] if purchase_block else None,
            "max_balance_usd": auth.MAX_CREDIT_BALANCE_USD,
            "headroom_usd": round(headroom / 1_000_000, 2),
        },
        "monthly_limit": auth.monthly_limit_view(row),
        "usage_month_usd": round(
            auth.usage_month_credit_micros(row["id"]) / 1_000_000, 2
        ),
        "history": auth.credit_history(row["id"]),
    }


@router.post("/refresh")
def billing_refresh(request: Request, user=Depends(auth.require_user)):
    """Self-heal: pull this user's state straight from Stripe. Called by the
    account page after a checkout redirect, so a delayed (or misrouted - see
    the Cloudflare notes in SETUP-BILLING.md) webhook can't strand a paying
    user. Idempotent: subscription mirroring overwrites in place and credits
    dedupe on the session id."""
    auth.require_same_origin(request)
    _require_configured()
    _rate_limit(user["id"])
    row = auth.get_user_by_id(user["id"])
    customer_id = row["stripe_customer_id"]
    if not customer_id:
        return {"ok": True, "refreshed": False}

    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
    best = None
    rank = {"active": 0, "trialing": 1, "past_due": 2}
    for sub in subs.get("data") or []:
        if best is None or rank.get(sub.get("status"), 9) < rank.get(best.get("status"), 9):
            best = sub
    if best is not None:
        _apply_subscription(row["id"], best)

    sessions = stripe.checkout.Session.list(customer=customer_id, limit=10)
    for session in sessions.get("data") or []:
        if session.get("mode") == "payment" and session.get("payment_status") == "paid":
            _credit_paid_session(row["id"], session)

    return {"ok": True, "refreshed": True}


# ---------- webhook (unauthenticated; signature-verified) ----------

@webhook_router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe calls this directly - no session cookie, no Origin header, so no
    require_user / require_same_origin. Authenticity comes from the signature
    over the RAW body; any proxy that rewrites the body breaks it, which is
    why Cloudflare must pass this path through untouched."""
    if not configured() or not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook is not configured.")
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, signature, STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        raise HTTPException(400, "Invalid webhook signature.")
    _handle_event(event)
    return {"received": True}


def _handle_event(event) -> None:
    """Apply one verified Stripe event. Every branch is idempotent, so Stripe's
    at-least-once delivery (and event replay from the dashboard) is safe.
    Unrecognized event types are acknowledged and ignored."""
    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}

    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        user_id = _user_id_from(obj)
        if user_id is None:
            return
        # A checkout is the one place a customer id is minted for users who
        # somehow lack one (e.g. a Payment Link created by hand in the
        # dashboard with client_reference_id set).
        if obj.get("customer"):
            auth.set_stripe_customer(user_id, obj["customer"])
        if obj.get("mode") == "payment" and obj.get("payment_status") == "paid":
            _credit_paid_session(user_id, obj)
        elif obj.get("mode") == "subscription" and obj.get("subscription"):
            sub = stripe.Subscription.retrieve(obj["subscription"])
            _apply_subscription(user_id, sub)

    elif etype in ("customer.subscription.created", "customer.subscription.updated",
                   "customer.subscription.deleted"):
        user_id = _user_id_from(obj)
        if user_id is None:
            return
        _apply_subscription(
            user_id, obj, deleted=(etype == "customer.subscription.deleted")
        )

    elif etype == "invoice.paid":
        # Renewals: refresh the paid-through date. The subscription id has
        # moved between API versions (invoice.subscription vs
        # invoice.parent.subscription_details.subscription); check both, and
        # fall back silently - customer.subscription.updated covers renewals
        # too, this is belt-and-suspenders.
        sub_id = obj.get("subscription") or (
            ((obj.get("parent") or {}).get("subscription_details") or {})
            .get("subscription")
        )
        if not sub_id:
            return
        sub = stripe.Subscription.retrieve(sub_id)
        user_id = _user_id_from(sub)
        if user_id is not None:
            _apply_subscription(user_id, sub)
