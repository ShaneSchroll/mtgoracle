"""Per-user saved content: conversation history and saved decks."""

from .db import _db, _now, _write_tx


# ---------- conversation history ----------
# Each user keeps only their most recent chats in the sidebar; older ones are
# pruned so the table can't grow without bound on the persistent disk.
MAX_CONVERSATIONS = 5


def save_conversation(
    user_id: int,
    conv_id: int | None,
    title: str,
    fmt: str,
    messages_json: str,
) -> int:
    """Insert a new conversation or update an existing one owned by user_id,
    then prune everything past the MAX_CONVERSATIONS most recent. Returns the
    conversation id (new or existing)."""
    now = _now().isoformat()
    with _db() as db:
        row = None
        if conv_id is not None:
            row = db.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()

        if row:
            db.execute(
                "UPDATE conversations SET title = ?, format = ?, messages = ?, "
                "updated_at = ? WHERE id = ?",
                (title, fmt, messages_json, now, row["id"]),
            )
            cid = row["id"]
        else:
            cur = db.execute(
                "INSERT INTO conversations (user_id, title, format, messages, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, title, fmt, messages_json, now, now),
            )
            cid = cur.lastrowid

        # Prune older conversations beyond the cap for this user.
        db.execute(
            """
            DELETE FROM conversations
             WHERE user_id = ?
               AND id NOT IN (
                 SELECT id FROM conversations
                  WHERE user_id = ?
                  ORDER BY updated_at DESC, id DESC
                  LIMIT ?
               )
            """,
            (user_id, user_id, MAX_CONVERSATIONS),
        )
        return cid


def list_conversations(user_id: int, limit: int = MAX_CONVERSATIONS) -> list[dict]:
    """The user's most recent conversations (metadata only, newest first)."""
    with _db() as db:
        rows = db.execute(
            "SELECT id, title, format, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "format": r["format"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def get_conversation(user_id: int, conv_id: int) -> dict | None:
    """Full conversation (including its stored messages) if owned by user_id."""
    with _db() as db:
        r = db.execute(
            "SELECT id, title, format, messages, updated_at FROM conversations "
            "WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "title": r["title"],
        "format": r["format"],
        "messages": r["messages"],
        "updated_at": r["updated_at"],
    }


def delete_conversation(user_id: int, conv_id: int) -> bool:
    with _db() as db:
        cur = db.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        return cur.rowcount > 0


# ---------- saved decks ----------
# Deliberately unlike conversations above: those silently prune past the cap,
# which is fine for a chat you can re-ask, but a decklist is work the user
# entered by hand. So there is no pruning here - save_deck refuses to create a
# third deck and the caller turns that into an "overwrite which slot?" prompt.
MAX_DECKS = 2


class DeckSlotsFull(Exception):
    """Raised by save_deck when the user already has MAX_DECKS and asked to
    create another. Carries the existing decks so the UI can offer them as
    overwrite targets."""

    def __init__(self, decks: list[dict]):
        super().__init__("All deck slots are in use.")
        self.decks = decks


def _deck_row(r, *, full: bool) -> dict:
    deck = {
        "id": r["id"],
        "name": r["name"],
        "format": r["format"],
        "commander": r["commander"],
        "allow_banned": bool(r["allow_banned"]),
        "updated_at": r["updated_at"],
    }
    if full:
        deck["cards"] = r["cards"]
        deck["goal"] = r["goal"]
        deck["messages"] = r["messages"]
    return deck


def save_deck(
    user_id: int,
    deck_id: int | None,
    name: str,
    fmt: str,
    commander: str,
    cards: str,
    goal: str,
    allow_banned: bool,
    messages_json: str,
) -> int:
    """Create or update one of the user's deck slots. Returns the deck id.

    Raises DeckSlotsFull when creating would exceed MAX_DECKS, so the caller can
    ask which slot to replace rather than silently dropping one."""
    now = _now().isoformat()
    with _db() as db, _write_tx(db):
        row = None
        if deck_id is not None:
            row = db.execute(
                "SELECT id FROM decks WHERE id = ? AND user_id = ?",
                (deck_id, user_id),
            ).fetchone()

        if row:
            db.execute(
                "UPDATE decks SET name = ?, format = ?, commander = ?, cards = ?, "
                "goal = ?, allow_banned = ?, messages = ?, updated_at = ? "
                "WHERE id = ?",
                (name, fmt, commander, cards, goal, int(allow_banned),
                 messages_json, now, row["id"]),
            )
            return row["id"]

        # Creating. Count inside the same write transaction so two concurrent
        # saves can't both see one free slot.
        (count,) = db.execute(
            "SELECT COUNT(*) FROM decks WHERE user_id = ?", (user_id,)
        ).fetchone()
        if count >= MAX_DECKS:
            rows = db.execute(
                "SELECT id, name, format, commander, allow_banned, updated_at "
                "FROM decks WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
                (user_id,),
            ).fetchall()
            raise DeckSlotsFull([_deck_row(r, full=False) for r in rows])

        cur = db.execute(
            "INSERT INTO decks (user_id, name, format, commander, cards, goal, "
            "allow_banned, messages, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, fmt, commander, cards, goal, int(allow_banned),
             messages_json, now, now),
        )
        return cur.lastrowid


def list_decks(user_id: int) -> list[dict]:
    """The user's saved decks (metadata only, newest first)."""
    with _db() as db:
        rows = db.execute(
            "SELECT id, name, format, commander, allow_banned, updated_at "
            "FROM decks WHERE user_id = ? ORDER BY updated_at DESC, id DESC "
            "LIMIT ?",
            (user_id, MAX_DECKS),
        ).fetchall()
    return [_deck_row(r, full=False) for r in rows]


def get_deck(user_id: int, deck_id: int) -> dict | None:
    """A full saved deck (list + session) if owned by user_id."""
    with _db() as db:
        r = db.execute(
            "SELECT id, name, format, commander, cards, goal, allow_banned, "
            "messages, updated_at FROM decks WHERE id = ? AND user_id = ?",
            (deck_id, user_id),
        ).fetchone()
    return _deck_row(r, full=True) if r else None


def delete_deck(user_id: int, deck_id: int) -> bool:
    with _db() as db:
        cur = db.execute(
            "DELETE FROM decks WHERE id = ? AND user_id = ?", (deck_id, user_id)
        )
        return cur.rowcount > 0
