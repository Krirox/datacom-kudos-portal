"""Kudos rules: sign-in, giving kudos, the public feed, and moderation.

The HTTP layer lives in server.py. This module is the part the specification
calls the source of truth.
"""

import hashlib
import hmac
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"
DEMO_PASSWORD = "kudos-demo"
MAX_MESSAGE_LENGTH = 500
MAX_REASON_LENGTH = 200
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
HOURLY_LIMIT = 20
DUPLICATE_WINDOW = timedelta(hours=24)
RATE_WINDOW = timedelta(hours=1)
HASH_ITERATIONS = 100_000

DEMO_USERS = (
    ("Alex Chen", "alex.chen@datacom.example", "employee"),
    ("Sam Patel", "sam.patel@datacom.example", "employee"),
    ("Jordan Lee", "jordan.lee@datacom.example", "employee"),
    ("Riley Morgan", "riley.morgan@datacom.example", "admin"),
)


class KudosError(Exception):
    """A failure the API should show to the caller."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _utc_now():
    return datetime.now(timezone.utc)


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value):
    return _as_utc(value).replace(microsecond=0).isoformat()


def hash_password(password, salt=None, iterations=HASH_ITERATIONS):
    """Return ``salt$hash`` using PBKDF2-HMAC-SHA256."""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    )
    return f"{salt}${digest.hex()}"


class KudosService:
    """SQLite-backed kudos operations."""

    def __init__(
        self,
        db_path,
        clock=None,
        iterations=HASH_ITERATIONS,
        hourly_limit=HOURLY_LIMIT,
        seed=True,
    ):
        self.db_path = str(db_path)
        self.clock = clock or _utc_now
        self.iterations = iterations
        self.hourly_limit = hourly_limit
        self._lock = threading.Lock()
        self._init_db(seed)

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _now(self):
        return _as_utc(self.clock())

    def _init_db(self, seed):
        with self._lock:
            connection = self._connect()
            try:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                connection.commit()
                existing = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
                if seed and existing == 0:
                    self._seed(connection)
                    connection.commit()
            finally:
                connection.close()

    def _seed(self, connection):
        created_at = _iso(self._now())
        ids = {}
        for name, email, role in DEMO_USERS:
            password_hash = hash_password(DEMO_PASSWORD, iterations=self.iterations)
            cursor = connection.execute(
                """
                INSERT INTO users (name, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, email, password_hash, role, created_at),
            )
            ids[email] = cursor.lastrowid
        samples = (
            (ids["alex.chen@datacom.example"], ids["sam.patel@datacom.example"],
             "Thanks for talking me through the deployment this morning."),
            (ids["jordan.lee@datacom.example"], ids["alex.chen@datacom.example"],
             "The notes you left on the incident review saved the next on-call a lot of time."),
        )
        for sender_id, recipient_id, message in samples:
            connection.execute(
                """
                INSERT INTO kudos (sender_id, recipient_id, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (sender_id, recipient_id, message, created_at),
            )

    def create_user(self, name, email, password, role="employee"):
        """Provision an account. The portal has no public registration route."""
        cleaned_name = (name or "").strip()
        cleaned_email = (email or "").strip().lower()
        if not cleaned_name or not cleaned_email or not password:
            raise KudosError(400, "Name, email, and password are required")
        if role not in ("employee", "admin"):
            raise KudosError(400, "Role must be employee or admin")
        password_hash = hash_password(password, iterations=self.iterations)
        connection = self._connect()
        try:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (name, email, password_hash, role, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cleaned_name, cleaned_email, password_hash, role, _iso(self._now())),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise KudosError(409, "An account with that email already exists") from exc
            row = connection.execute(
                "SELECT id, name, email, role FROM users WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return _public_user(row)
        finally:
            connection.close()

    def authenticate(self, email, password):
        cleaned_email = (email or "").strip().lower()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ?",
                (cleaned_email,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            hash_password(password or "", salt="0" * 32, iterations=self.iterations)
            raise KudosError(401, "Invalid email or password")
        salt = row["password_hash"].split("$", 1)[0]
        candidate = hash_password(password or "", salt=salt, iterations=self.iterations)
        if not hmac.compare_digest(candidate, row["password_hash"]):
            raise KudosError(401, "Invalid email or password")
        return _public_user(row)

    def get_user(self, user_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, name, email, role FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KudosError(401, "Sign in required")
        return _public_user(row)

    def list_colleagues(self, user_id):
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT id, name, email FROM users
                WHERE id <> ?
                ORDER BY name COLLATE NOCASE, id
                """,
                (user_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def give_kudos(self, sender_id, recipient_id, message):
        recipient_id = _require_int(recipient_id, "Choose a colleague")
        cleaned = _clean_message(message)
        if recipient_id == sender_id:
            raise KudosError(400, "You cannot give kudos to yourself")
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            recipient = connection.execute(
                "SELECT id FROM users WHERE id = ?",
                (recipient_id,),
            ).fetchone()
            if recipient is None:
                raise KudosError(400, "Choose a colleague from the list")
            duplicate_after = _iso(now - DUPLICATE_WINDOW)
            recent = connection.execute(
                """
                SELECT message FROM kudos
                WHERE sender_id = ? AND recipient_id = ? AND created_at >= ?
                """,
                (sender_id, recipient_id, duplicate_after),
            ).fetchall()
            folded = cleaned.casefold()
            if any(row["message"].casefold() == folded for row in recent):
                raise KudosError(409, "You already sent this kudos recently")
            hour_ago = _iso(now - RATE_WINDOW)
            sent_this_hour = connection.execute(
                """
                SELECT COUNT(*) AS n FROM kudos
                WHERE sender_id = ? AND created_at >= ?
                """,
                (sender_id, hour_ago),
            ).fetchone()["n"]
            if sent_this_hour >= self.hourly_limit:
                raise KudosError(429, "You have sent too many kudos this hour")
            cursor = connection.execute(
                """
                INSERT INTO kudos (sender_id, recipient_id, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (sender_id, recipient_id, cleaned, _iso(now)),
            )
            connection.commit()
            kudos_id = cursor.lastrowid
        except KudosError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise KudosError(400, "Kudos could not be saved") from exc
        finally:
            connection.close()
        return self._get_kudos(kudos_id)

    def public_feed(self, page, page_size):
        page, page_size = _paging(page, page_size)
        return self._list_kudos(page, page_size, visible_only=True)

    def admin_feed(self, actor_id, page, page_size):
        self._require_admin(actor_id)
        page, page_size = _paging(page, page_size)
        return self._list_kudos(page, page_size, visible_only=False)

    def hide_kudos(self, actor_id, kudos_id, reason):
        self._require_admin(actor_id)
        kudos_id = _require_int(kudos_id, "Kudos was not found")
        cleaned_reason = (reason or "").strip()
        if not cleaned_reason or len(cleaned_reason) > MAX_REASON_LENGTH:
            raise KudosError(400, "A reason of up to 200 characters is required")
        now = _iso(self._now())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._lock_kudos(connection, kudos_id)
            connection.execute(
                """
                UPDATE kudos
                SET is_visible = 0,
                    moderated_by = ?,
                    moderated_at = ?,
                    reason_for_moderation = ?
                WHERE id = ?
                """,
                (actor_id, now, cleaned_reason, kudos_id),
            )
            self._record_event(
                connection, row, "hide", actor_id, cleaned_reason, now
            )
            connection.commit()
        except KudosError:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._get_kudos(kudos_id)

    def restore_kudos(self, actor_id, kudos_id):
        self._require_admin(actor_id)
        kudos_id = _require_int(kudos_id, "Kudos was not found")
        now = _iso(self._now())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._lock_kudos(connection, kudos_id)
            if row["is_visible"]:
                raise KudosError(400, "Kudos is already visible")
            connection.execute(
                """
                UPDATE kudos
                SET is_visible = 1,
                    moderated_by = NULL,
                    moderated_at = NULL,
                    reason_for_moderation = NULL
                WHERE id = ?
                """,
                (kudos_id,),
            )
            self._record_event(connection, row, "restore", actor_id, None, now)
            connection.commit()
        except KudosError:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._get_kudos(kudos_id)

    def delete_kudos(self, actor_id, kudos_id):
        self._require_admin(actor_id)
        kudos_id = _require_int(kudos_id, "Kudos was not found")
        now = _iso(self._now())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._lock_kudos(connection, kudos_id)
            self._record_event(connection, row, "delete", actor_id, None, now)
            connection.execute("DELETE FROM kudos WHERE id = ?", (kudos_id,))
            connection.commit()
        except KudosError:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"ok": True}

    def moderation_events(self):
        """Return audit rows. Used by tests and by an operator inspecting the file."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT id, kudos_id, action, actor_id, reason, message_snapshot, created_at
                FROM moderation_events
                ORDER BY id
                """
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def _require_admin(self, actor_id):
        user = self.get_user(actor_id)
        if user["role"] != "admin":
            raise KudosError(403, "You do not have permission to moderate kudos")
        return user

    def _lock_kudos(self, connection, kudos_id):
        row = connection.execute(
            "SELECT * FROM kudos WHERE id = ?",
            (kudos_id,),
        ).fetchone()
        if row is None:
            raise KudosError(404, "Kudos was not found")
        return row

    def _record_event(self, connection, row, action, actor_id, reason, created_at):
        connection.execute(
            """
            INSERT INTO moderation_events (
                kudos_id, action, actor_id, reason, message_snapshot, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row["id"], action, actor_id, reason, row["message"], created_at),
        )

    def _list_kudos(self, page, page_size, visible_only):
        where = "WHERE k.is_visible = 1" if visible_only else ""
        offset = (page - 1) * page_size
        connection = self._connect()
        try:
            total = connection.execute(
                f"SELECT COUNT(*) AS n FROM kudos k {where}"
            ).fetchone()["n"]
            rows = connection.execute(
                f"""
                {_KUDOS_SELECT}
                {where}
                ORDER BY k.created_at DESC, k.id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        finally:
            connection.close()
        return {
            "items": [_kudos_dict(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def _get_kudos(self, kudos_id):
        connection = self._connect()
        try:
            row = connection.execute(
                f"{_KUDOS_SELECT} WHERE k.id = ?",
                (kudos_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KudosError(404, "Kudos was not found")
        return _kudos_dict(row)


_KUDOS_SELECT = """
SELECT
    k.id, k.sender_id, k.recipient_id, k.message, k.created_at, k.is_visible,
    k.moderated_by, k.moderated_at, k.reason_for_moderation,
    s.name AS sender_name,
    r.name AS recipient_name,
    m.name AS moderated_by_name
FROM kudos k
JOIN users s ON s.id = k.sender_id
JOIN users r ON r.id = k.recipient_id
LEFT JOIN users m ON m.id = k.moderated_by
"""


def _public_user(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
    }


def _kudos_dict(row):
    return {
        "id": row["id"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "recipient_id": row["recipient_id"],
        "recipient_name": row["recipient_name"],
        "message": row["message"],
        "created_at": row["created_at"],
        "is_visible": bool(row["is_visible"]),
        "moderated_by": row["moderated_by"],
        "moderated_by_name": row["moderated_by_name"],
        "moderated_at": row["moderated_at"],
        "reason_for_moderation": row["reason_for_moderation"],
    }


def _clean_message(message):
    cleaned = (message or "").strip()
    if not cleaned:
        raise KudosError(400, "Write a short message of appreciation")
    if len(cleaned) > MAX_MESSAGE_LENGTH:
        raise KudosError(400, "The message must be 500 characters or fewer")
    return cleaned


def _require_int(value, message):
    if isinstance(value, bool) or not isinstance(value, int):
        raise KudosError(400, message)
    return value


def _paging(page, page_size):
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise KudosError(400, "Page must be a positive whole number")
    if page_size is None:
        page_size = DEFAULT_PAGE_SIZE
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise KudosError(400, "Page size must be a whole number")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise KudosError(400, f"Page size must be between 1 and {MAX_PAGE_SIZE}")
    return page, page_size
