"""Local mock GitHub (GraphQL) and GitLab (REST) servers on 127.0.0.1. Stdlib only."""
from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from datetime import datetime, timezone


def iso(t):
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MockServer:
    """handler(req) -> (status, headers_dict, body_bytes|str|obj). req is a dict."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                parsed = urllib.parse.urlparse(self.path)
                req = {"method": self.command, "path": parsed.path, "query": urllib.parse.parse_qs(parsed.query),
                       "raw_path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                       "body": body, "port": outer.port}
                outer.requests.append(req)
                status, headers, payload = outer.handler(req)
                if not isinstance(payload, (bytes, str)):
                    payload = json.dumps(payload)
                if isinstance(payload, str):
                    payload = payload.encode()
                self.send_response(status)
                headers = dict(headers or {})
                headers.setdefault("Content-Type", "application/json")
                headers["Content-Length"] = str(len(payload))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _do

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


# ----------------------------------------------------------------- GitHub

def gh_node(number, title, created, merged=None, closed=None, state=None, base="main", head=None, author="alice",
            author_type="User", additions=10, deletions=2, files=1, commits=1, first_commit=None, merge_oid=None,
            labels=(), reviews=(), ready=None, draft=False):
    """reviews: list of (login, type, state, submitted_ts)."""
    state = state or ("MERGED" if merged else "OPEN")
    return {
        "number": number, "title": title, "state": state, "isDraft": draft, "createdAt": iso(created),
        "mergedAt": iso(merged), "closedAt": iso(closed or merged), "additions": additions, "deletions": deletions,
        "changedFiles": files, "headRefName": head or f"feat/b{number}", "baseRefName": base,
        "author": {"login": author, "__typename": author_type},
        "labels": {"nodes": [{"name": l} for l in labels]},
        "mergeCommit": {"oid": merge_oid} if merge_oid else None,
        "commits": {"totalCount": commits, "nodes": [{"commit": {"authoredDate": iso(first_commit or created)}}]},
        "reviews": {"nodes": [{"submittedAt": iso(ts), "state": st, "author": {"login": lg, "__typename": ty}}
                              for lg, ty, st, ts in reviews]},
        "timelineItems": {"nodes": [{"createdAt": iso(ready)}] if ready else []},
    }


def github_handler(nodes, page_size=3, status_override=None, expect_token=None):
    """Serve nodes (already ordered newest-created first) in pages of page_size via GraphQL cursors."""

    def handler(req):
        if status_override:
            return status_override(req)
        if expect_token and req["headers"].get("authorization") != f"Bearer {expect_token}":
            return 401, {}, {"message": "Bad credentials"}
        body = json.loads(req["body"] or b"{}")
        cursor = body.get("variables", {}).get("cursor")
        start = int(cursor) if cursor else 0
        page = nodes[start:start + page_size]
        nxt = start + page_size
        return 200, {}, {"data": {"repository": {"pullRequests": {
            "pageInfo": {"hasNextPage": nxt < len(nodes), "endCursor": str(nxt)}, "nodes": page}}}}

    return handler


# ----------------------------------------------------------------- GitLab

def gl_mr(iid, title, created, merged=None, closed=None, state=None, source="feat/x", author="alice",
          author_bot=False, merge_sha=None, squash_sha=None, labels=()):
    state = state or ("merged" if merged else "opened")
    return {"iid": iid, "title": title, "state": state, "created_at": iso(created), "merged_at": iso(merged),
            "closed_at": iso(closed), "source_branch": source, "target_branch": "main",
            "author": {"username": author, "bot": author_bot}, "merge_commit_sha": merge_sha,
            "squash_commit_sha": squash_sha, "labels": list(labels), "draft": False}


def gitlab_handler(mrs, commits=None, notes=None, page_size=2, status_override=None):
    commits = commits or {}
    notes = notes or {}

    def handler(req):
        if status_override:
            return status_override(req)
        if req["headers"].get("private-token") is None:
            return 401, {}, {"message": "401 Unauthorized"}
        path = urllib.parse.unquote(req["path"])
        if path.endswith("/merge_requests"):
            page = int(req["query"].get("page", ["1"])[0])
            chunk = mrs[(page - 1) * page_size: page * page_size]
            more = page * page_size < len(mrs)
            return 200, {"X-Next-Page": str(page + 1) if more else ""}, chunk
        parts = path.rstrip("/").split("/")
        if parts[-1] == "commits":
            return 200, {}, commits.get(int(parts[-2]), [])
        if parts[-1] == "notes":
            return 200, {}, notes.get(int(parts[-2]), [])
        return 404, {}, {"message": "404 Not Found"}

    return handler


def gl_note(author, ts, system=False, bot=False):
    return {"author": {"username": author, "bot": bot}, "created_at": iso(ts), "system": system}


def gl_commit(ts):
    return {"authored_date": iso(ts), "created_at": iso(ts)}
