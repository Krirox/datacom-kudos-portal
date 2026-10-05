"""HTTP adapter for the kudos service."""

import json
import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kudos import DEFAULT_PAGE_SIZE, KudosError

logger = logging.getLogger("kudos")
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SESSION_SECONDS = 8 * 3600
MAX_BODY = 16_384
_ID_ACTION = re.compile(r"^/api/kudos/(\d+)/(hide|restore)$")
_ID_DELETE = re.compile(r"^/api/kudos/(\d+)$")


class SessionStore:
    """In-memory sessions. A process restart signs everyone out."""

    def __init__(self, ttl_seconds=SESSION_SECONDS, clock=None):
        self.ttl_seconds = ttl_seconds
        self.clock = clock or time.time
        self._items = {}
        self._lock = threading.Lock()

    def create(self, user_id):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[token] = (user_id, self.clock() + self.ttl_seconds)
        return token

    def read(self, token):
        if not token:
            return None
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            user_id, expires = item
            if self.clock() >= expires:
                self._items.pop(token, None)
                return None
            return user_id

    def delete(self, token):
        if not token:
            return
        with self._lock:
            self._items.pop(token, None)


def make_handler(service, sessions):
    """Build a request handler bound to one service and one session store."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            logger.info("%s %s", self.address_string(), fmt % args)

        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def do_DELETE(self):
            self._guard(self._delete)

        def _guard(self, method):
            try:
                method()
            except KudosError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except Exception:
                logger.exception("unhandled error")
                self._send_json(500, {"error": "Something went wrong"})

        def _get(self):
            path, params = _split(self.path)
            if path == "/":
                self._send_file(STATIC / "index.html")
                return
            if path == "/favicon.ico":
                self._send_empty(204)
                return
            if path.startswith("/static/"):
                relative = path[len("/static/"):]
                candidate = (STATIC / relative).resolve()
                if not candidate.is_relative_to(STATIC.resolve()) or not candidate.is_file():
                    self._send_json(404, {"error": "Not found"})
                    return
                self._send_file(candidate)
                return
            if path == "/api/me":
                self._send_json(200, service.get_user(self._require_user()))
                return
            if path == "/api/users":
                self._send_json(200, service.list_colleagues(self._require_user()))
                return
            if path == "/api/kudos":
                user_id = self._require_user()
                service.get_user(user_id)
                page = _query_int(params, "page", 1)
                page_size = _query_int(params, "page_size", DEFAULT_PAGE_SIZE)
                self._send_json(200, service.public_feed(page, page_size))
                return
            if path == "/api/admin/kudos":
                user_id = self._require_user()
                page = _query_int(params, "page", 1)
                page_size = _query_int(params, "page_size", DEFAULT_PAGE_SIZE)
                self._send_json(200, service.admin_feed(user_id, page, page_size))
                return
            self._send_json(404, {"error": "Not found"})

        def _post(self):
            path, _params = _split(self.path)
            payload = self._read_json()
            if path == "/api/login":
                self._login(payload)
                return
            if path == "/api/logout":
                sessions.delete(_cookie(self.headers.get("Cookie"), "session"))
                self._send_json(200, {"ok": True}, [_clear_cookie()])
                return
            user_id = self._require_user()
            if path == "/api/kudos":
                created = service.give_kudos(
                    user_id, payload.get("recipient_id"), payload.get("message")
                )
                logger.info("kudos created id=%s sender=%s", created["id"], user_id)
                self._send_json(201, created)
                return
            match = _ID_ACTION.match(path)
            if match:
                kudos_id = int(match.group(1))
                action = match.group(2)
                if action == "hide":
                    updated = service.hide_kudos(user_id, kudos_id, payload.get("reason"))
                    logger.info("moderation action=hide actor=%s kudos=%s", user_id, kudos_id)
                    self._send_json(200, updated)
                    return
                updated = service.restore_kudos(user_id, kudos_id)
                logger.info("moderation action=restore actor=%s kudos=%s", user_id, kudos_id)
                self._send_json(200, updated)
                return
            self._send_json(404, {"error": "Not found"})

        def _delete(self):
            path, _params = _split(self.path)
            match = _ID_DELETE.match(path)
            if not match:
                self._send_json(404, {"error": "Not found"})
                return
            user_id = self._require_user()
            kudos_id = int(match.group(1))
            result = service.delete_kudos(user_id, kudos_id)
            logger.info("moderation action=delete actor=%s kudos=%s", user_id, kudos_id)
            self._send_json(200, result)

        def _login(self, payload):
            email = payload.get("email")
            try:
                user = service.authenticate(email, payload.get("password"))
            except KudosError as exc:
                if exc.status == 401:
                    logger.info(
                        "sign-in failed email=%s",
                        (email or "").strip().lower(),
                    )
                raise
            token = sessions.create(user["id"])
            self._send_json(200, user, [_session_cookie(token)])

        def _require_user(self):
            user_id = sessions.read(_cookie(self.headers.get("Cookie"), "session"))
            if user_id is None:
                raise KudosError(401, "Sign in required")
            return user_id

        def _read_json(self):
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                return {}
            try:
                length = int(length_header)
            except ValueError as exc:
                raise KudosError(400, "Request body must be JSON") from exc
            if length < 0 or length > MAX_BODY:
                raise KudosError(400, "Request is too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                raise KudosError(400, "Content-Type must be application/json")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KudosError(400, "Request body must be JSON") from exc
            if not isinstance(payload, dict):
                raise KudosError(400, "Request body must be a JSON object")
            return payload

        def _send_json(self, status, payload, extra_headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            for name, value in extra_headers or []:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path):
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
            }.get(path.suffix, "application/octet-stream")
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_empty(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()

        def _security_headers(self):
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")

    return Handler


def serve(service, host="127.0.0.1", port=8000, sessions=None):
    """Listen until the caller closes the returned server."""
    store = sessions or SessionStore()
    server = ThreadingHTTPServer((host, port), make_handler(service, store))
    server.sessions = store
    return server


def _split(raw_path):
    parsed = urlparse(raw_path)
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path, parse_qs(parsed.query)


def _query_int(params, name, default):
    values = params.get(name)
    if not values or values[0] == "":
        return default
    try:
        return int(values[0])
    except ValueError as exc:
        raise KudosError(400, "Page must be a positive whole number") from exc


def _cookie(header, name):
    if not header:
        return None
    prefix = name + "="
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix):]
    return None


def _session_cookie(token):
    return (
        "Set-Cookie",
        f"session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_SECONDS}",
    )


def _clear_cookie():
    return ("Set-Cookie", "session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
