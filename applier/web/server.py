"""The local GUI server.

Deliberately built on the standard library. A web framework would be three or
four more packages in the dependency tree of something that holds a passport
number, an immigration status and a keychain handle — and it would buy nothing,
because this serves one user on one machine over loopback. Everything here is
`http.server` plus about two hundred lines of routing.

The threading server matters for one reason: an SSE stream occupies its thread
for as long as the browser watches a task, which for the autonomous loop can be
hours. A single-threaded server would be deaf for the duration.
"""

from __future__ import annotations

import json
import mimetypes
import re
import socket
import threading
import traceback
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..config import project_paths
from . import api
from .security import (
    MAX_BODY_BYTES, SECURITY_HEADERS, TOKEN_HEADER, authorize, is_loopback, new_token,
)
from .tasks import RUNNER

STATIC = Path(__file__).resolve().parent / "static"

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

# (method, compiled path, handler, param names)
ROUTES: list[tuple[str, re.Pattern, object]] = []


def route(method: str, pattern: str):
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise api.ApiError("expected a numeric id", 400)


# --------------------------------------------------------------------------- #
# route table — one line per endpoint, all logic lives in api.py
# --------------------------------------------------------------------------- #
route("GET", "/api/state")(api.get_state)
route("GET", "/api/events")(api.get_events)
route("POST", "/api/doctor")(api.run_doctor)
route("GET", "/api/paths")(api.get_paths)
route("POST", "/api/audit")(api.run_privacy_audit)

route("GET", "/api/jobs")(api.list_jobs)
route("GET", "/api/jobs/{job_id}")(lambda b, job_id, **k: api.get_job(b, job_id=_int(job_id), **k))
route("POST", "/api/jobs/{job_id}/status")(lambda b, job_id, **k: api.set_job_status(b, job_id=_int(job_id), **k))
route("POST", "/api/jobs/{job_id}/apply")(lambda b, job_id, **k: api.start_apply_job(b, job_id=_int(job_id), **k))

route("POST", "/api/discover")(api.start_discover)
route("POST", "/api/rank")(api.start_rank)
route("POST", "/api/apply")(api.start_apply)
route("POST", "/api/run")(api.start_run)

route("GET", "/api/tasks")(api.list_tasks)
route("GET", "/api/tasks/{task_id}")(lambda b, task_id, **k: api.get_task(b, task_id=task_id, **k))
route("POST", "/api/tasks/{task_id}/stop")(lambda b, task_id, **k: api.stop_task(b, task_id=task_id, **k))

route("GET", "/api/applications")(api.list_applications)
route("GET", "/api/applications/{app_id}")(lambda b, app_id, **k: api.get_application(b, app_id=_int(app_id), **k))
route("POST", "/api/applications/{app_id}/outcome")(lambda b, app_id, **k: api.set_outcome(b, app_id=_int(app_id), **k))

route("GET", "/api/asks")(api.list_asks)
route("POST", "/api/asks/{ask_id}")(lambda b, ask_id, **k: api.answer_ask(b, ask_id=_int(ask_id), **k))
route("DELETE", "/api/asks/{ask_id}")(lambda b, ask_id, **k: api.dismiss_ask(b, ask_id=_int(ask_id), **k))

route("GET", "/api/answers")(api.list_answers)
route("PATCH", "/api/answers/{answer_id}")(lambda b, answer_id, **k: api.update_answer(b, answer_id=_int(answer_id), **k))
route("DELETE", "/api/answers/{answer_id}")(lambda b, answer_id, **k: api.delete_answer(b, answer_id=_int(answer_id), **k))

route("GET", "/api/bank")(api.get_bank)
route("PUT", "/api/bank")(api.save_bank)
route("POST", "/api/bank/import")(api.import_bank)
route("POST", "/api/bank/preview")(api.preview_bank)

route("GET", "/api/outreach")(api.list_contacts)
route("POST", "/api/outreach/harvest")(api.start_harvest)
route("POST", "/api/outreach/draft")(api.start_draft)
route("POST", "/api/outreach/{contact_id}/stage")(lambda b, contact_id, **k: api.set_contact_stage(b, contact_id=_int(contact_id), **k))

route("GET", "/api/settings")(api.get_settings)
route("PUT", "/api/settings")(api.put_settings)
route("POST", "/api/autonomy")(api.set_autonomy)
route("GET", "/api/profile")(api.get_profile)
route("PUT", "/api/profile")(api.put_profile)
route("GET", "/api/secrets")(api.get_secrets)
route("PUT", "/api/secrets")(api.put_secret)


# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "applier"
    sys_version = ""                     # do not advertise the Python version
    protocol_version = "HTTP/1.1"

    token: str = ""
    port: int = 0

    # ----------------------------------------------------------- plumbing --
    def log_message(self, fmt, *args):   # noqa: A003 - base class name
        if self.server_instance.verbose:
            print(f"  [web] {fmt % args}")

    @property
    def server_instance(self):
        return self.server.applier_server

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # --------------------------------------------------------------- verbs --
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")

    def do_OPTIONS(self):
        # No CORS headers, on purpose: every cross-origin preflight fails, so
        # a foreign page cannot send the custom token header at all.
        self._send(405, b"", "text/plain")

    # -------------------------------------------------------------- router --
    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        denied = authorize(method=method, path=path, headers=self.headers,
                           port=self.port, token=self.token)
        if denied:
            # One generic message. Distinguishing "bad token" from "bad host"
            # in the reply would let a prober map the defences.
            self.log_message("refused %s %s: %s", method, path, denied)
            return self._error(403, "refused")

        if not path.startswith("/api/"):
            return self._static(path, method)

        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if method == "GET" and re.fullmatch(r"/api/tasks/([^/]+)/stream", path):
            return self._stream(path.split("/")[3], query)
        if method == "GET" and path == "/api/artifact":
            return self._artifact(query)

        body = None
        if method in ("POST", "PUT", "PATCH"):
            try:
                body = self._read_body()
            except api.ApiError as e:
                return self._error(e.status, e.message)

        for m, rx, fn in ROUTES:
            if m != method:
                continue
            match = rx.match(path)
            if not match:
                continue
            try:
                result = fn(body, query=query, **match.groupdict())
                return self._json(200, result)
            except api.ApiError as e:
                return self._error(e.status, e.message)
            except Exception as e:                       # noqa: BLE001
                traceback.print_exc()
                return self._error(500, f"{type(e).__name__}: {e}")

        self._error(404, f"no route for {method} {path}")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise api.ApiError("request body too large", 413)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype and ctype != "application/json":
            raise api.ApiError("send application/json", 415)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise api.ApiError("body is not valid JSON", 400)
        if not isinstance(parsed, dict):
            raise api.ApiError("body must be a JSON object", 400)
        return parsed

    # -------------------------------------------------------------- static --
    def _static(self, path: str, method: str) -> None:
        if method != "GET":
            return self._error(405, "method not allowed")
        rel = "index.html" if path in ("/", "") else path.lstrip("/")

        # `rel` comes straight off the URL. On Windows a path like "/C:/Windows"
        # would otherwise make `STATIC / rel` an absolute path elsewhere on the
        # disk, so containment is checked after resolution, not before.
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            return self._error(404, "not found")

        # Windows resolves MIME types through the registry, where .js and .css
        # are routinely mapped to something unhelpful by whatever was installed
        # last. A browser with strict MIME checking then refuses the file, so
        # the types the UI depends on are pinned here rather than guessed.
        ctype = STATIC_TYPES.get(target.suffix.lower()) \
            or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"

        return self._send(200, target.read_bytes(), ctype,
                          {"Cache-Control": "no-store"})

    # ------------------------------------------------------------ artifacts --
    def _artifact(self, query: dict) -> None:
        """Serve one generated file — a resume PDF, a screenshot, a report.

        The client only ever names a path relative to the artifacts root, and
        the result is re-resolved against that root and checked. Symlinks
        resolve before the check, so a link planted inside an artifact
        directory cannot be used to walk out of it.
        """
        rel = (query.get("rel") or "").strip()
        if not rel or "\x00" in rel:
            return self._error(400, "rel is required")
        root = project_paths()["artifacts"].resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            return self._error(403, "outside the artifacts directory")
        if not target.is_file():
            return self._error(404, "no such artifact")
        if target.suffix.lower() not in api.SAFE_SUFFIXES:
            return self._error(415, f"{target.suffix} is not a previewable type")

        ctype, _ = mimetypes.guess_type(target.name)
        if target.suffix.lower() in (".txt", ".tex", ".md", ".log"):
            ctype = "text/plain; charset=utf-8"
        elif target.suffix.lower() == ".html":
            # Never rendered: an employer's saved page is untrusted HTML and
            # this origin holds the session token.
            ctype = "text/plain; charset=utf-8"
        return self._send(200, target.read_bytes(),
                          ctype or "application/octet-stream",
                          {"Cache-Control": "no-store",
                           "Content-Disposition": f'inline; filename="{target.name}"'})

    # ---------------------------------------------------------------- SSE ---
    def _stream(self, task_id: str, query: dict) -> None:
        task = RUNNER.get(task_id)
        if not task:
            return self._error(404, "no such task")
        try:
            cursor = int(query.get("cursor", 0))
        except ValueError:
            cursor = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            for chunk in RUNNER.stream(task, cursor):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass          # the browser navigated away; nothing to clean up
        self.close_connection = True


# --------------------------------------------------------------------------- #
class GuiServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 *, verbose: bool = False) -> None:
        if not is_loopback(host):
            raise ValueError(
                f"refusing to bind {host!r}: the GUI serves a profile containing "
                f"a date of birth, a home address and an immigration status, and "
                f"has no authentication beyond a per-launch token. Loopback only.")
        self.host = host
        self.port = _free_port(host, port)
        self.token = new_token()
        self.verbose = verbose
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def entry_url(self) -> str:
        """The URL to actually open: the token rides in the fragment.

        A fragment is never sent to the server, so the token stays out of
        access logs, out of the Referer header, and out of anything a proxy
        might record. The page moves it into sessionStorage and clears it
        from the address bar on first load.
        """
        return f"{self.url}#t={self.token}"

    def serve_forever(self, *, open_browser: bool = True) -> None:
        handler = partial(Handler)
        Handler.token = self.token
        Handler.port = self.port
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        httpd.daemon_threads = True
        httpd.applier_server = self
        self._httpd = httpd

        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(self.entry_url)).start()
        try:
            httpd.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
        finally:
            RUNNER.stop_all()
            httpd.server_close()

    def shutdown(self) -> None:
        if self._httpd:
            self._httpd.shutdown()


def _free_port(host: str, preferred: int) -> int:
    """Take the preferred port, or the next free one after it."""
    for candidate in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError(f"no free port in {preferred}..{preferred + 40}")
