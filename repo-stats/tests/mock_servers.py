"""Local mock GitHub (GraphQL) and GitLab (REST) servers on 127.0.0.1. Stdlib only."""
from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

# A recorded request: method, path, query, raw_path, headers (lower-cased names), body and port.
Request = Dict[str, Any]
# A handler's answer: (status, headers or None, payload as bytes, str or a JSON-serialisable object).
Response = Tuple[int, Optional[Mapping[str, str]], Any]
Handler = Callable[[Request], Response]


def iso(t: float | None) -> str | None:
    """Format a Unix time as an ISO-8601 UTC string; None stays None."""
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MockServer:
    """A threaded HTTP server on 127.0.0.1 that answers every request with `handler`.

    The handler receives a request dict (method, path, query, raw_path, headers, body, port) and
    returns (status, headers, payload); a payload that is not bytes or str is sent as JSON.
    Every request is also recorded in `requests`.
    """

    def __init__(self, handler: Handler) -> None:
        """Bind to a free loopback port; call start() or use `with` to begin serving."""
        self.handler = handler
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            """Request handler that records each request and delegates the response to `handler`."""

            protocol_version = "HTTP/1.0"

            def log_message(self, format: str, *args: Any) -> None:
                """Silence the default per-request logging to stderr."""
                pass

            def _do(self) -> None:
                """Read and record the request, then write the handler's response."""
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
    def url(self) -> str:
        """Base URL of the server, e.g. http://127.0.0.1:PORT."""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> MockServer:
        """Start serving in a daemon thread and return self."""
        self.thread.start()
        return self

    def stop(self) -> None:
        """Shut the server down and close its socket."""
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> MockServer:
        """Start the server for a `with` block."""
        return self.start()

    def __exit__(self, *a: object) -> None:
        """Stop the server when the `with` block ends."""
        self.stop()


# ----------------------------------------------------------------- GitHub

def gh_node(number: int, title: str, created: int, merged: int | None = None, closed: int | None = None,
            state: str | None = None, base: str = "main", head: str | None = None, author: str = "alice",
            author_type: str = "User", additions: int = 10, deletions: int = 2, files: int = 1, commits: int = 1,
            first_commit: int | None = None, merge_oid: str | None = None, labels: Iterable[str] = (),
            reviews: Iterable[tuple[Any, ...]] = (), ready: int | None = None,
            draft: bool = False, review_total: int | None = None) -> dict[str, Any]:
    """Build one GitHub GraphQL pullRequest node with the fields repo_stats queries.

    Times are Unix seconds. The state is MERGED when `merged` is set and OPEN otherwise, unless
    `state` is given.

    Args:
        reviews: (login, author __typename, review state, submitted time[, reviewed commit oid]) tuples.
            Without an oid every review is of the same commit "c0".
        ready: Time of the ready-for-review event, if any.
        review_total: reviews.totalCount; defaults to the number of reviews given.
    """
    state = state or ("MERGED" if merged else "OPEN")
    reviews = list(reviews)
    return {
        "number": number, "title": title, "state": state, "isDraft": draft, "createdAt": iso(created),
        "mergedAt": iso(merged), "closedAt": iso(closed or merged), "additions": additions, "deletions": deletions,
        "changedFiles": files, "headRefName": head or f"feat/b{number}", "baseRefName": base,
        "author": {"login": author, "__typename": author_type},
        "labels": {"nodes": [{"name": l} for l in labels]},
        "mergeCommit": {"oid": merge_oid} if merge_oid else None,
        "commits": {"totalCount": commits, "nodes": [{"commit": {"authoredDate": iso(first_commit or created)}}]},
        "reviews": {"totalCount": len(reviews) if review_total is None else review_total,
                    "nodes": [{"submittedAt": iso(r[3]), "state": r[2], "commit": {"oid": r[4] if len(r) > 4 else "c0"},
                               "author": {"login": r[0], "__typename": r[1]}} for r in reviews]},
        "timelineItems": {"nodes": [{"createdAt": iso(ready)}] if ready else []},
    }


def github_handler(nodes: Sequence[Mapping[str, Any]], page_size: int = 3, status_override: Handler | None = None,
                   expect_token: str | None = None) -> Handler:
    """Serve nodes (already ordered newest-created first) in pages of page_size via GraphQL cursors.

    Args:
        status_override: When set, answers every request instead.
        expect_token: When set, requests without this bearer token get 401.
    """

    def handler(req: Request) -> Response:
        """Answer one GraphQL request with the page selected by its cursor variable."""
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

def gl_mr(iid: int, title: str, created: int, merged: int | None = None, closed: int | None = None,
          state: str | None = None, source: str = "feat/x", author: str = "alice", author_bot: bool = False,
          merge_sha: str | None = None, squash_sha: str | None = None, labels: Iterable[str] = ()) -> dict[str, Any]:
    """Build one GitLab REST merge request object.

    Times are Unix seconds. The state is merged when `merged` is set and opened otherwise, unless
    `state` is given.
    """
    state = state or ("merged" if merged else "opened")
    return {"iid": iid, "title": title, "state": state, "created_at": iso(created), "merged_at": iso(merged),
            "closed_at": iso(closed), "source_branch": source, "target_branch": "main",
            "author": {"username": author, "bot": author_bot}, "merge_commit_sha": merge_sha,
            "squash_commit_sha": squash_sha, "labels": list(labels), "draft": False}


def gitlab_handler(mrs: Sequence[Mapping[str, Any]], commits: Mapping[int, list[dict[str, Any]]] | None = None,
                   notes: Mapping[int, list[dict[str, Any]]] | None = None, page_size: int = 2,
                   status_override: Handler | None = None) -> Handler:
    """Serve GitLab merge request lists (paged via X-Next-Page) and per-MR commits and notes.

    Args:
        mrs: Merge requests in the order the list endpoint returns them.
        commits: Map of MR iid to its commit objects.
        notes: Map of MR iid to its note objects.
        page_size: Merge requests per list page.
        status_override: When set, answers every request instead.
    """
    commits = commits or {}
    notes = notes or {}

    def handler(req: Request) -> Response:
        """Route one GitLab API request; requests without a PRIVATE-TOKEN header get 401."""
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


def gl_note(author: str, ts: int, system: bool = False, bot: bool = False, body: str = "") -> dict[str, Any]:
    """Build one GitLab note (MR comment, or a system note such as "added 1 commit") object."""
    return {"author": {"username": author, "bot": bot}, "created_at": iso(ts), "system": system, "body": body}


def gl_push(author: str, ts: int, n: int = 1) -> dict[str, Any]:
    """Build the GitLab system note written when `author` pushes `n` commits to the MR branch."""
    return gl_note(author, ts, system=True, body=f"added {n} commit{'s' if n != 1 else ''}\n\n<ul><li>abc - msg</li></ul>")


def gl_approval(author: str, ts: int) -> dict[str, Any]:
    """Build the GitLab system note written when `author` approves the MR."""
    return gl_note(author, ts, system=True, body="approved this merge request")


def gl_commit(ts: int) -> dict[str, Any]:
    """Build one GitLab MR commit object authored and created at `ts`."""
    return {"authored_date": iso(ts), "created_at": iso(ts)}
