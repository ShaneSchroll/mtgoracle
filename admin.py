"""
admin.py - Command-line user management for the MTG Rules Oracle.

Run on the same host the server runs on (so it touches the same users.db).

  python admin.py list
  python admin.py create  alice@example.com [--admin] [--approved]
  python admin.py approve alice@example.com | --all
  python admin.py revoke  alice@example.com
  python admin.py make-admin alice@example.com
  python admin.py reset   alice@example.com [--base-url https://oracle.example.com]
  python admin.py delete  alice@example.com [--yes]
  python admin.py credits alice@example.com 5 [--note "beta tester"]
  python admin.py comp    alice@example.com on|off

Purchased credits are non-refundable and capped at auth.MAX_CREDIT_BALANCE_USD
per account; `credits` is the owner override for both (grants bypass the cap,
and a negative amount claws back, e.g. to mirror a chargeback settled in
Stripe).

Bootstrap: the very first time you deploy, create your own admin user with
  python admin.py create you@example.com --admin --approved
Registration is open after that - accounts arrive usable, gated by the
subscription + credits, not by a human. `revoke` suspends an account and
`approve` puts it back, so approval is moderation rather than an intake queue.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from app import auth


def cmd_list(_):
    users = auth.list_users()
    if not users:
        print("(no users)")
        return
    print(f"{'EMAIL':36} {'ACTIVE':7} {'ADMIN':6} {'BUDGET/MO':12} {'SUB':10} {'CREDITS':10} CREATED")
    for u in users:
        raw = u["monthly_budget_micros"]
        if raw is None:
            budget = "default"
        elif raw < 0:
            budget = "unlimited"
        else:
            budget = f"${raw / 1_000_000:.2f}"
        sub = u["subscription_status"] or "-"
        balance = f"${auth.credit_balance_micros(u['id']) / 1_000_000:.2f}"
        print(
            f"{u['email']:36} "
            f"{'yes' if u['approved'] else 'NO':7} "
            f"{'yes' if u['is_admin'] else 'no':6} "
            f"{budget:12} "
            f"{sub:10} "
            f"{balance:10} "
            f"{u['created_at']}"
        )


def cmd_create(args):
    pw = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw != pw2:
        sys.exit("Passwords do not match.")
    try:
        uid = auth.create_user(args.email, pw)
    except Exception as e:
        sys.exit(f"Failed: {e}")
    if args.approved:
        auth.set_approved(args.email, True)
    if args.admin:
        auth.set_admin(args.email, True)
    print(
        f"Created user id={uid} email={args.email} "
        f"approved={bool(args.approved)} admin={bool(args.admin)}"
    )


def cmd_approve(args):
    """Reinstate a suspended account. Signups arrive approved, so this is
    normally the undo for `revoke` - plus `--all`, which sweeps up accounts
    left at approved=0 by the old approval-queue registration flow."""
    if args.all:
        reinstated = [u["email"] for u in auth.list_users() if not u["approved"]]
        if not reinstated:
            print("No suspended accounts.")
            return
        for email in reinstated:
            auth.set_approved(email, True)
        print(f"Reinstated {len(reinstated)} account(s): {', '.join(reinstated)}")
        return
    if not args.email:
        sys.exit("Specify an email, or --all to reinstate every suspended account.")
    if auth.set_approved(args.email, True):
        print(f"Approved {args.email}")
    else:
        sys.exit(f"No such user: {args.email}")


def cmd_revoke(args):
    """Suspend an account: they keep their credits and subscription but can't
    sign in or use the app until `approve` puts them back."""
    if auth.set_approved(args.email, False):
        print(f"Revoked access for {args.email}")
    else:
        sys.exit(f"No such user: {args.email}")


def cmd_make_admin(args):
    if auth.set_admin(args.email, True):
        print(f"{args.email} is now admin")
    else:
        sys.exit(f"No such user: {args.email}")


def cmd_reset(args):
    user = auth.get_user_by_email(args.email.lower())
    if not user:
        sys.exit(f"No such user: {args.email}")
    token = auth.create_reset_token(user["id"])
    # `or` rather than a getenv default: APP_BASE_URL set to "" is a real
    # possibility, and it would print a reset link with no host in it.
    base = args.base_url or os.getenv("APP_BASE_URL") or "http://localhost:8000"
    print("Single-use reset link (valid for 24 hours):")
    print(f"  {base.rstrip('/')}/reset?token={token}")


def cmd_delete(args):
    if not args.yes:
        ans = input(f"Delete {args.email}? [y/N] ")
        if ans.strip().lower() != "y":
            print("Aborted")
            return
    if auth.delete_user(args.email):
        print(f"Deleted {args.email}")
    else:
        sys.exit(f"No such user: {args.email}")


def _fmt_usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


def cmd_budget(args):
    """Override a user's monthly spend limit, in credit dollars. Users set
    their own on /account (seeded from their balance at first purchase); this
    is the admin path, and the only one that can clear the limit or lift it
    entirely."""
    if args.unlimited:
        micros = -1
    elif args.default:
        micros = None
    elif args.usd is not None:
        if args.usd < 0:
            sys.exit("Use --unlimited for no cap; --usd must be >= 0.")
        micros = int(round(args.usd * 1_000_000))
    else:
        sys.exit("Specify one of --usd N, --unlimited, or --default.")

    if not auth.set_monthly_budget(args.email, micros):
        sys.exit(f"No such user: {args.email}")

    if micros is None:
        print(f"{args.email}: monthly budget reset to default "
              f"({_fmt_usd(auth.DEFAULT_MONTHLY_BUDGET_MICROS)}/month).")
    elif micros < 0:
        print(f"{args.email}: monthly budget set to UNLIMITED.")
    else:
        print(f"{args.email}: monthly budget set to {_fmt_usd(micros)}/month.")


def cmd_usage(args):
    """This month's spend against the monthly limit. Both are CREDIT dollars
    (what the user's balance drops by); `raw cost` is what we pay Anthropic."""
    s = auth.usage_summary_month(args.email)
    if s is None:
        sys.exit(f"No such user: {args.email}")
    print(f"{s['email']} - usage this month (resets on the 1st, 00:00 UTC):")
    print(f"  spent:     {_fmt_usd(s['spent_micros'])} of credits")
    print(f"  raw cost:  {_fmt_usd(s['raw_micros'])}")
    if s["unlimited"]:
        print("  limit:     unlimited")
    else:
        print(f"  limit:     {_fmt_usd(s['budget_micros'])}")
        print(f"  remaining: {_fmt_usd(s['remaining_micros'])}")


def cmd_credits(args):
    """Grant (or claw back, with a negative amount) prepaid usage credits."""
    user = auth.get_user_by_email(args.email.lower())
    if not user:
        sys.exit(f"No such user: {args.email}")
    if not auth.grant_credits(args.email, args.usd, args.note):
        sys.exit(f"Failed to grant credits to {args.email}")
    balance = auth.credit_balance_micros(user["id"])
    verb = "Granted" if args.usd >= 0 else "Clawed back"
    print(f"{verb} ${abs(args.usd):.2f}. {args.email} balance: {_fmt_usd(balance)}")


def cmd_comp(args):
    """Grant/revoke complimentary subscription status (passes the subscription
    gate without Stripe; usage still deducts credits)."""
    on = args.state == "on"
    if not auth.set_comp(args.email, on):
        sys.exit(f"No such user: {args.email}")
    print(f"{args.email}: comp subscription {'granted' if on else 'revoked'}.")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI's argument parser, with no side effects.

    Split out of main() so the admin panel can introspect the real parser when
    it renders its buttons and its shell-command reference - which is what stops
    the panel from ever drifting from the CLI. main() keeps the auth.init_db()
    call: merely listing the commands must not create or migrate a database.
    """
    p = argparse.ArgumentParser(description="MTG Oracle user admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    sp = sub.add_parser("create")
    sp.add_argument("email")
    sp.add_argument("--admin", action="store_true")
    sp.add_argument("--approved", action="store_true")
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("approve", help="Reinstate a suspended account")
    sp.add_argument("email", nargs="?")
    sp.add_argument("--all", action="store_true",
                    help="Reinstate every suspended account (e.g. signups left "
                         "pending by the old approval queue)")
    sp.set_defaults(func=cmd_approve)

    for name, func, helptext in [
        ("revoke", cmd_revoke, "Suspend an account"),
        ("make-admin", cmd_make_admin, "Grant admin rights"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("email")
        sp.set_defaults(func=func)

    sp = sub.add_parser("reset")
    sp.add_argument("email")
    sp.add_argument("--base-url", help="Base URL for the reset link (default $APP_BASE_URL or http://localhost:8000)")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("delete")
    sp.add_argument("email")
    sp.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("budget", help="Set a user's monthly spend limit")
    sp.add_argument("email")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--usd", type=float, help="Monthly limit in credit dollars, e.g. 2.50")
    g.add_argument("--unlimited", action="store_true", help="No monthly cap")
    g.add_argument("--default", action="store_true", help="Use the global default")
    sp.set_defaults(func=cmd_budget)

    sp = sub.add_parser("usage", help="Show a user's spend so far this month")
    sp.add_argument("email")
    sp.set_defaults(func=cmd_usage)

    sp = sub.add_parser("credits", help="Grant prepaid usage credits (negative claws back)")
    sp.add_argument("email")
    sp.add_argument("usd", type=float, help="Dollar amount, e.g. 5 or -2.50")
    sp.add_argument("--note", default=None, help="Optional ledger note")
    sp.set_defaults(func=cmd_credits)

    sp = sub.add_parser("comp", help="Grant/revoke complimentary subscription")
    sp.add_argument("email")
    sp.add_argument("state", choices=["on", "off"])
    sp.set_defaults(func=cmd_comp)

    return p


def main():
    auth.init_db()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
