"""A pretend employer, so the submit path can be exercised without one.

    python scripts/fake_employer.py          # serves until Ctrl-C
    python scripts/fake_employer.py --once   # serves one application, prints it

The one genuinely irreversible thing this system does is submit an
application, and that was also the one path never run end to end -- because
running it means sending a real application to a real company. The usual
resolution is to read the code very carefully and hope. This is the other
resolution: an employer that exists only on loopback.

The form deliberately includes the parts that break filling in practice:

* a file input, since resume upload is the step most likely to fail silently
* a free-text question, which has to be answered by the model rather than
  copied from the profile
* a work-authorisation question, which must come from the legal resolver
  exactly and never be guessed
* a required field with no obvious label, which is what the universal filler
  exists for
* a demographic question that is legitimately declinable

Nothing here is a mock of applier's own code. It is an HTTP server with an
HTML form, and the real browser automation drives it exactly as it would
drive Greenhouse.
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JOB_TITLE = "Software Engineering Intern, Summer 2027"
COMPANY = "Loopback Industries"

JOB_PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{JOB_TITLE} - {COMPANY}</title></head><body>
<h1>{JOB_TITLE}</h1>
<h2>{COMPANY}</h2>
<p>We are hiring a software engineering intern for Summer 2027. You will work
in Python and TypeScript on backend services, REST APIs and PostgreSQL, with
automated testing throughout. We value engineers who have shipped something
end to end on their own.</p>
<p>Requirements: currently enrolled undergraduate, graduating between December
2027 and June 2028. Strong fundamentals in algorithms and data structures.</p>
<p>We welcome international students and support CPT and OPT.</p>
<p><a href="/apply" id="apply-link">Apply for this job</a></p>
</body></html>"""

FORM_PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Apply - {JOB_TITLE}</title></head><body>
<h1>Apply: {JOB_TITLE}</h1>
<form method="POST" action="/apply" enctype="multipart/form-data" id="application">
  <label for="first_name">First name *</label>
  <input type="text" id="first_name" name="first_name" required><br>

  <label for="last_name">Last name *</label>
  <input type="text" id="last_name" name="last_name" required><br>

  <label for="email">Email *</label>
  <input type="email" id="email" name="email" required><br>

  <label for="phone">Phone</label>
  <input type="tel" id="phone" name="phone"><br>

  <label for="resume">Resume (PDF) *</label>
  <input type="file" id="resume" name="resume" accept=".pdf" required><br>

  <label for="linkedin">LinkedIn profile</label>
  <input type="url" id="linkedin" name="linkedin"><br>

  <label for="school">School *</label>
  <input type="text" id="school" name="school" required><br>

  <!-- No label element at all: this is what the universal filler is for. -->
  <p>Expected graduation (YYYY-MM)</p>
  <input type="text" name="grad" id="grad"><br>

  <label for="why">Why do you want to work here? *</label>
  <textarea id="why" name="why" rows="5" required></textarea><br>

  <label for="work_auth">Are you legally authorised to work in the United States? *</label>
  <select id="work_auth" name="work_auth" required>
    <option value="">Select...</option>
    <option value="yes">Yes</option>
    <option value="no">No</option>
  </select><br>

  <label for="sponsorship">Will you now or in the future require sponsorship? *</label>
  <select id="sponsorship" name="sponsorship" required>
    <option value="">Select...</option>
    <option value="yes">Yes</option>
    <option value="no">No</option>
  </select><br>

  <label for="gender">Gender (optional)</label>
  <select id="gender" name="gender">
    <option value="">Select...</option>
    <option value="male">Male</option>
    <option value="female">Female</option>
    <option value="decline">I don't wish to answer</option>
  </select><br>

  <button type="submit" id="submit-application">Submit application</button>
</form>
</body></html>"""

RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    once = False
    # Serve the failure instead of the thank-you. This is the case the
    # record could not previously distinguish: the click lands, the form
    # comes back with a validation summary at the same address, and the
    # application is written down as submitted. Being able to produce it on
    # demand is the only way to check that it is read correctly.
    reject = False

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):                                     # noqa: N802
        if self.path.startswith("/apply"):
            self._send(FORM_PAGE)
        elif self.path in ("/", "/job"):
            self._send(JOB_PAGE)
        else:
            self._send("<h1>404</h1>", 404)

    def do_POST(self):                                    # noqa: N802
        # `cgi` was removed in Python 3.13, so the multipart body is parsed
        # with the email package, which is what cgi used underneath anyway.
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        got: dict = {}
        if ctype.startswith("multipart/"):
            parser = email.parser.BytesParser(policy=email.policy.default)
            message = parser.parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                if filename:
                    got[name] = {
                        "filename": filename,
                        "bytes": len(payload),
                        "looks_like_pdf": payload[:4] == b"%PDF",
                    }
                else:
                    got[name] = payload.decode("utf-8", "replace")
        else:
            from urllib.parse import parse_qs
            for k, v in parse_qs(raw.decode("utf-8", "replace")).items():
                got[k] = v[0] if len(v) == 1 else v
        RECEIVED.append(got)

        print("\n--- application received ---", flush=True)
        print(json.dumps(got, indent=2)[:4000], flush=True)

        if type(self).reject:
            # What a real board does: the form again, with the complaint on
            # top, at the URL it was already at.
            self._send("<h1>There was an error</h1>"
                       "<p>Phone number is required. Please correct the "
                       "errors below and resubmit.</p>" + FORM_PAGE, 200)
            return
        self._send(f"<h1>Thanks</h1><p>Application received for "
                   f"{html.escape(JOB_TITLE)}.</p>")
        if type(self).once:
            raise SystemExit(0)

    def log_message(self, fmt, *args):                    # noqa: D102
        print(f"  [employer] {fmt % args}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8990)
    ap.add_argument("--once", action="store_true",
                    help="Exit after one submitted application.")
    ap.add_argument("--reject", action="store_true",
                    help="Answer every submission with a validation error, "
                         "so the unconfirmed path can be exercised.")
    ap.add_argument("--dump", type=Path,
                    help="Write what was received to this JSON file.")
    args = ap.parse_args()

    Handler.once = args.once
    Handler.reject = args.reject
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"pretend employer on http://127.0.0.1:{args.port}/job", flush=True)
    print("  apply with:  python -m applier apply "
          f"http://127.0.0.1:{args.port}/job --review", flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        if args.dump:
            args.dump.write_text(json.dumps(RECEIVED, indent=2), encoding="utf-8")
            print(f"wrote {args.dump}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
