"""Dashboard and JSON API, served with the standard library HTTP server."""
from __future__ import annotations

import hmac
import io
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

from . import __version__, attachments, auth, csvio, db, ics, invoices, links, reminders, store
from .config import Settings
from .notify import get_notifier, get_webhook

MAX_BODY = 64 * 1024
MAX_IMPORT = 1024 * 1024
MAX_UPLOAD = attachments.MAX_BYTES
DASHBOARD_FILE = Path(__file__).with_name("dashboard.html")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, errors: Optional[dict[str, str]] = None, extra: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or {}
        self.extra = extra or {}


def make_server(settings: Settings, db_path: str, host: str, port: int, notifier=None) -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS and not settings.api_token:
        raise ValueError("Set THINGSEXPIRE_TOKEN before listening on a non-local address.")
    db.connect(db_path).close()  # create/migrate once up front
    mailer = notifier
    files = attachments.files_dir(db_path, settings.attachments_dir)
    throttle = auth.LoginThrottle()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ThingsExpireTracker"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep test and CLI output quiet
            pass

        # ---- plumbing -------------------------------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict[str, str]] = None) -> None:
            self.send_response(status)
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _principal(self, conn: sqlite3.Connection, query_token: str = "") -> Optional[dict[str, str]]:
            """Who is calling: the shared API token acts as an admin; otherwise a logged-in user's session."""
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else query_token
            if settings.api_token and supplied and hmac.compare_digest(supplied.encode("utf-8"), settings.api_token.encode("utf-8")):
                return {"name": "token", "role": "admin"}
            if auth.has_users(conn):
                user = auth.user_for_token(conn, supplied)
                return {"name": user["username"], "role": user["role"], "id": user["id"]} if user else None
            if settings.api_token:
                return None
            return {"name": "api", "role": "admin"}  # no token and no users: a private local install

        def _raw_body(self, limit: int) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "Bad Content-Length.") from None
            if length < 0 or length > limit:
                raise ApiError(413, "Request body is too large.")
            return self.rfile.read(length) if length else b""

        def _body(self) -> dict[str, Any]:
            raw = self._raw_body(MAX_BODY)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "Body must be valid JSON.") from None
            if not isinstance(data, dict):
                raise ApiError(400, "Body must be a JSON object.")
            return data

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            if method == "GET" and path == "/":
                page = DASHBOARD_FILE.read_text(encoding="utf-8").replace("__APP__", _esc(settings.app_name))
                self._send(200, page.replace("__CURRENCY__", _esc(settings.currency)).encode("utf-8"), "text/html; charset=utf-8")
                return
            if method == "GET" and path == "/api/health":
                self._json(200, {"status": "ok", "version": __version__})
                return
            try:
                if path == "/ack" and method in ("GET", "POST"):
                    self._ack(method, query)
                    return
                if not path.startswith("/api/"):
                    raise ApiError(404, "Not found.")
                conn = db.connect(db_path)
                try:
                    status, payload, extra = self._handle_api(conn, method, path, query)
                finally:
                    conn.close()
                if isinstance(payload, tuple):  # (content type, text or bytes) for downloads
                    data = payload[1] if isinstance(payload[1], bytes) else payload[1].encode("utf-8")
                    self._send(status, data, payload[0], extra)
                else:
                    self._json(status, payload)
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message, "fields": exc.errors, **exc.extra})
            except store.ValidationError as exc:
                self._json(422, {"error": "Validation failed.", "fields": exc.errors})
            except (attachments.AttachmentError, auth.AuthError) as exc:
                self._json(422, {"error": str(exc), "fields": {}})
            except store.NotFound as exc:
                self._json(404, {"error": str(exc), "fields": {}})
            except sqlite3.Error as exc:
                print(f"database error: {exc}", file=sys.stderr)
                self._json(500, {"error": "Database error.", "fields": {}})
            except Exception as exc:  # noqa: BLE001 - never leak internals to the client
                print(f"unexpected error: {exc!r}", file=sys.stderr)
                self._json(500, {"error": "Something went wrong.", "fields": {}})

        def _handle_api(self, conn, method, path, query):
            q1 = lambda key: (query.get(key) or [""])[0]  # noqa: E731
            if path == "/api/auth/status" and method == "GET":
                return 200, {"login_required": auth.has_users(conn), "token_required": bool(settings.api_token)}, None
            if path == "/api/login" and method == "POST":
                data = self._body()
                name = str(data.get("username") or "")
                if throttle.blocked(name):
                    raise ApiError(429, "Too many attempts. Wait a minute and try again.")
                try:
                    token, user = auth.login(conn, name, str(data.get("password") or ""))
                except auth.AuthError as exc:
                    throttle.failed(name)
                    raise ApiError(401, str(exc)) from None
                throttle.succeeded(name)
                return 200, {"token": token, "user": user}, None

            # Calendar apps cannot send headers, so the .ics feed (and only it) also accepts ?token=.
            who = self._principal(conn, q1("token") if path == "/api/calendar.ics" else "")
            if who is None:
                raise ApiError(401, "Please sign in." if auth.has_users(conn) else "Missing or wrong token.")
            needed = "admin" if path.startswith("/api/users") else ("viewer" if method == "GET" else "editor")
            if path in ("/api/logout", "/api/me"):
                needed = "viewer"
            if not auth.allows(who["role"], needed):
                raise ApiError(403, f"This needs the {needed} role.")

            body: dict[str, Any] = {}
            raw_upload = path == "/api/import" or path.endswith("/attachments") or path == "/api/invoices/upload"
            if method == "POST" and raw_upload:
                body = {"raw": self._raw_body(MAX_IMPORT if path == "/api/import" else MAX_UPLOAD)}
            elif method in ("POST", "PUT"):
                body = self._body()
            status, payload = self._route(conn, method, path, query, body, who)
            extra = None
            if isinstance(payload, tuple) and len(payload) == 3:
                payload, extra = (payload[0], payload[1]), payload[2]
            return status, payload, extra

        def _ack(self, method: str, query) -> None:
            """The page behind the email link. GET only shows a button, so mail scanners that open links change nothing."""
            q1 = lambda key: (query.get(key) or [""])[0]  # noqa: E731
            conn = db.connect(db_path)
            try:
                message, ok = "This link is no longer valid. It may have expired, or the item was renewed.", False
                try:
                    item = store.get_item(conn, int(q1("i")))
                    days = int(q1("d"))
                    if 1 <= days <= store.MAX_SNOOZE_DAYS and links.verify(settings, item, days, int(q1("x")), q1("s")):
                        if method == "POST":
                            store.snooze_item(conn, item["id"], days, actor="email-link")
                            message, ok = f"Thanks. Reminders for {item['name']} are paused for {days} days.", True
                        else:
                            message = f"Pause reminders for {item['name']} for {days} days?"
                            ok = None
                except (ValueError, store.NotFound):
                    pass
            finally:
                conn.close()
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            button = f"<form method='post' action='/ack?{_esc(qs)}'><button>Pause reminders</button></form>" if ok is None else ""
            page = (f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    f"<title>{_esc(settings.app_name)}</title><body style='font:16px system-ui;max-width:460px;margin:12vh auto;padding:0 20px'>"
                    f"<h2>{_esc(settings.app_name)}</h2><p>{_esc(message)}</p>{button}</body>")
            self._send(200 if ok is not False else 400, page.encode("utf-8"), "text/html; charset=utf-8")

        def _route(self, conn, method, path, query, body, who):
            actor = who["name"]
            parts = path.split("/")[2:]  # after /api
            q = lambda key: (query.get(key) or [""])[0]  # noqa: E731

            if parts == ["items"]:
                if method == "GET":
                    within = q("within_days")
                    items = store.list_items(
                        conn,
                        query=q("q"),
                        category=q("category"),
                        status=q("status"),
                        include_archived=q("archived") == "1",
                        within_days=int(within) if within.lstrip("-").isdigit() else None,
                    )
                    return 200, {"items": items}
                if method == "POST":
                    return 201, store.create_item(conn, body, actor=actor)
            if parts == ["summary"] and method == "GET":
                data = store.summary(conn)
                data["currency"] = settings.currency
                return 200, data
            if parts == ["activity"] and method == "GET":
                limit = q("limit")
                return 200, {"activity": store.recent_activity(conn, int(limit) if limit.isdigit() else 30)}
            if parts == ["export.csv"] and method == "GET":
                return 200, ("text/csv; charset=utf-8", csvio.export_csv(store.list_items(conn, include_archived=True)))
            if parts == ["calendar.ics"] and method == "GET":
                return 200, ("text/calendar; charset=utf-8", ics.build_calendar(store.list_items(conn), settings.app_name))
            if parts == ["import"] and method == "POST":
                try:
                    text = body["raw"].decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise ApiError(400, "The file must be UTF-8 text.") from None
                result = csvio.import_csv(conn, io.StringIO(text, newline=""), strict=q("strict") == "1", actor=actor)
                return 200, {"imported": result.imported, "skipped_duplicates": result.skipped_duplicates, "errors": result.errors}
            if parts == ["remind"] and method == "POST":
                try:
                    hook = get_webhook(settings)
                except ValueError as exc:
                    raise ApiError(500, str(exc)) from None
                report = reminders.run(
                    conn, mailer or get_notifier(settings), date.today(),
                    default_recipient=settings.default_recipient,
                    app_name=settings.app_name,
                    dry_run=bool(body.get("dry_run")),
                    escalate_to=settings.escalate_to,
                    webhook=hook,
                    currency=settings.currency,
                    link_for=(lambda item: links.snooze_link(settings, item)) if links.enabled(settings) else None,
                )
                return 200, {
                    "sent": report.sent, "failed": report.failed, "items_notified": report.items_notified,
                    "webhook_sent": report.webhook_sent, "webhook_failed": report.webhook_failed,
                    "no_recipient": report.no_recipient,
                    "planned": [
                        {"item": p.item["name"], "to": p.recipient or "webhook", "stage": p.stage, "escalation": p.escalation}
                        for p in report.planned
                    ],
                }
            if parts == ["me"] and method == "GET":
                return 200, {"name": who["name"], "role": who["role"], "login_required": auth.has_users(conn)}
            if parts == ["logout"] and method == "POST":
                header = self.headers.get("Authorization", "")
                auth.logout(conn, header[7:] if header.startswith("Bearer ") else "")
                return 200, {"ok": True}
            if parts == ["users"]:
                if method == "GET":
                    return 200, {"users": auth.list_users(conn)}
                if method == "POST":
                    return 201, auth.create_user(conn, str(body.get("username") or ""), str(body.get("password") or ""),
                                                 str(body.get("role") or "viewer"))
            if len(parts) == 2 and parts[0] == "users" and parts[1].isdigit() and method == "PUT":
                return 200, auth.update_user(conn, int(parts[1]), role=body.get("role"), disabled=body.get("disabled"),
                                             password=body.get("password"))
            if len(parts) == 2 and parts[0] == "attachments" and parts[1].isdigit():
                if method == "GET":
                    meta, data = attachments.read(conn, files, int(parts[1]))
                    disposition = "attachment; filename*=UTF-8''" + quote(meta["filename"])
                    return 200, (meta["content_type"], data, {"Content-Disposition": disposition})
                if method == "DELETE":
                    attachments.delete(conn, files, int(parts[1]), actor)
                    return 200, {"deleted": True}
            if parts == ["invoices"] and method == "GET":
                iid = q("item_id")
                return 200, {"invoices": invoices.list_invoices(conn, item_id=int(iid) if iid.isdigit() else None, status=q("status"))}
            if parts == ["invoices", "upload"] and method == "POST":
                item = q("item_id")
                try:
                    draft = invoices.ingest(conn, files, settings, q("filename"), body["raw"],
                                            item_id=int(item) if item.isdigit() else None, actor=actor)
                except invoices.InvoiceNeedsItem as exc:
                    raise ApiError(409, str(exc), extra={"needs_item": True, "fields": exc.result["fields"], "notes": exc.result["notes"]}) from None
                return 201, draft
            if len(parts) >= 2 and parts[0] == "invoices" and parts[1].isdigit():
                iid = int(parts[1])
                if len(parts) == 2 and method == "GET":
                    return 200, invoices.get(conn, iid)
                if len(parts) == 2 and method == "PUT":
                    return 200, invoices.update_draft(conn, iid, body)
                if len(parts) == 2 and method == "DELETE":
                    invoices.discard(conn, files, iid, actor)
                    return 200, {"deleted": True}
                if len(parts) == 3 and parts[2] == "confirm" and method == "POST":
                    return 200, invoices.confirm(conn, iid, update_cost=bool(body.get("update_cost")),
                                                 vendor_to_item=bool(body.get("vendor_to_item")), actor=actor)
            if len(parts) >= 2 and parts[0] == "items" and parts[1].isdigit():
                item_id = int(parts[1])
                if len(parts) == 2:
                    if method == "GET":
                        item = store.get_item(conn, item_id)
                        item["renewals"] = store.renewals_for(conn, item_id)
                        item["history"] = store.history_for(conn, item_id)
                        item["reminders"] = store.reminders_for(conn, item_id)
                        item["attachments"] = attachments.list_for(conn, item_id)
                        item["invoices"] = invoices.list_invoices(conn, item_id=item_id)
                        return 200, item
                    if method == "PUT":
                        return 200, store.update_item(conn, item_id, body, actor=actor)
                    if method == "DELETE":
                        store.get_item(conn, item_id)
                        store.set_archived(conn, item_id, True, actor=actor)
                        return 200, {"archived": True}
                if len(parts) == 3 and parts[2] == "renew" and method == "POST":
                    months = body.get("months")
                    if months is not None and (isinstance(months, bool) or not isinstance(months, int)):
                        raise ApiError(422, "Validation failed.", {"months": "Must be a whole number."})
                    return 200, store.renew_item(
                        conn, item_id,
                        new_expires_on=body.get("new_expires_on"),
                        months=months,
                        note=str(body.get("note") or ""),
                        actor=actor,
                    )
                if len(parts) == 3 and parts[2] == "unarchive" and method == "POST":
                    store.get_item(conn, item_id)
                    store.set_archived(conn, item_id, False, actor=actor)
                    return 200, store.get_item(conn, item_id)
                if len(parts) == 3 and parts[2] == "snooze" and method == "POST":
                    days = body.get("days")
                    return 200, store.snooze_item(conn, item_id, links.SNOOZE_DAYS if days is None else days, actor=actor)
                if len(parts) == 3 and parts[2] == "workflow" and method == "POST":
                    return 200, store.set_workflow(conn, item_id, str(body.get("state") or ""), actor=actor)
                if len(parts) == 3 and parts[2] == "attachments":
                    if method == "GET":
                        store.get_item(conn, item_id)
                        return 200, {"attachments": attachments.list_for(conn, item_id)}
                    if method == "POST":
                        return 201, attachments.save(conn, files, item_id, q("filename"), body["raw"],
                                                     kind=q("kind") or "document", actor=actor)
            raise ApiError(404, "Not found.")

        def do_GET(self): self._dispatch("GET")  # noqa: E704
        def do_POST(self): self._dispatch("POST")  # noqa: E704
        def do_PUT(self): self._dispatch("PUT")  # noqa: E704
        def do_DELETE(self): self._dispatch("DELETE")  # noqa: E704

    return ThreadingHTTPServer((host, port), Handler)


def serve(settings: Settings, db_path: str, host: str, port: int, out=None) -> None:
    server = make_server(settings, db_path, host, port)
    if out is not None:
        shown = server.server_address[1]
        print(f"{settings.app_name} dashboard on http://{host}:{shown}  (Ctrl+C to stop)", file=out)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
