"""Fake `gh` and `glab` for the tests. Stdlib only.

Each call turns one `gh api` / `glab api` invocation into one HTTP request to the mock server named by
REPO_STATS_FAKE_URL, and prints the answer the way the real CLI does with --include: status line, headers,
blank line, body. The exit status is 0 for 2xx and 1 otherwise; `gh` exits 4 when it has no token.

Only the arguments repo-stats is allowed to use are accepted, so a test fails if the code ever sends
anything else (-F, --input, --form, another method, a missing --hostname). When REPO_STATS_FAKE_LOG is set,
every call appends {"tool", "argv", "cwd", "env"} to it as one JSON line.

Credentials are found as the real CLIs find them: gh reads GH_TOKEN, then GITHUB_TOKEN, for github.com
and *.ghe.com, and GH_ENTERPRISE_TOKEN / GITHUB_ENTERPRISE_TOKEN for any other host; glab reads
GITLAB_TOKEN, GITLAB_ACCESS_TOKEN or OAUTH_TOKEN, and with GLAB_ENABLE_CI_AUTOLOGIN=true in GitLab CI
(GITLAB_CI=true) it sends CI_JOB_TOKEN as JOB-TOKEN.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Usage(Exception):
    """An argument the real repo-stats must never pass."""


def _parse(tool: str, argv: List[str]) -> Tuple[str, Optional[str], Dict[str, str]]:
    """Return (endpoint, hostname, -f fields) or raise Usage for anything outside the allowed set."""
    if not argv or argv[0] != "api":
        raise Usage(f"only `{tool} api` is allowed, got {argv[:1]}")
    endpoint = None
    hostname = None
    method = None
    include = False
    fields: Dict[str, str] = {}
    it = iter(argv[1:])
    for a in it:
        if a == "--hostname":
            hostname = next(it)
        elif a == "--include":
            include = True
        elif a == "--method" and tool == "glab":
            method = next(it)
        elif a == "-f":
            key, eq, value = next(it).partition("=")
            if not eq:
                raise Usage("-f needs key=value")
            fields[key] = value
        elif a.startswith("-"):
            raise Usage(f"flag not allowed: {a}")
        elif endpoint is None:
            endpoint = a
        else:
            raise Usage(f"unexpected argument {a}")
    if endpoint is None or not hostname or not include:
        raise Usage("endpoint, --hostname and --include are required")
    if tool == "gh" and endpoint != "graphql":
        raise Usage("gh may only call graphql")
    if tool == "glab" and method != "GET":
        raise Usage("glab must use --method GET")
    return endpoint, hostname, fields


def _log(tool: str, argv: List[str]) -> None:
    """Append this call to REPO_STATS_FAKE_LOG, if set."""
    path = os.environ.get("REPO_STATS_FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": tool, "argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")


def _first(*names: str) -> Optional[str]:
    """Return the first non-empty environment variable among names."""
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def main(tool: str, argv: List[str]) -> int:
    """Run one fake CLI call and return its exit status."""
    _log(tool, argv)
    try:
        endpoint, host, fields = _parse(tool, argv)
    except (Usage, StopIteration) as e:
        print(f"{tool}: usage error: {e}", file=sys.stderr)
        return 2
    headers = {"User-Agent": f"fake-{tool}"}
    if tool == "gh":
        public = host == "github.com" or bool(host and host.endswith(".ghe.com"))
        token = _first("GH_TOKEN", "GITHUB_TOKEN") if public else _first("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
        if not token:
            print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr)
            return 4
        headers["Authorization"] = f"Bearer {token}"
        query = fields.pop("query", "")
        data: Optional[bytes] = json.dumps({"query": query, "variables": fields}).encode()
        headers["Content-Type"] = "application/json"
        path = "/graphql"
    else:
        token = _first("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")
        if token:
            headers["PRIVATE-TOKEN"] = token
        elif (os.environ.get("GLAB_ENABLE_CI_AUTOLOGIN") == "true" and os.environ.get("GITLAB_CI") == "true"
              and os.environ.get("CI_JOB_TOKEN")):
            headers["JOB-TOKEN"] = os.environ["CI_JOB_TOKEN"]
        data = None
        path = "/api/v4/" + endpoint + ("?" + urllib.parse.urlencode(fields) if fields else "")
    base = os.environ.get("REPO_STATS_FAKE_URL")
    if not base:
        print(f"{tool}: no mock server (REPO_STATS_FAKE_URL is not set)", file=sys.stderr)
        return 1
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers)
    try:
        resp = _OPENER.open(req, timeout=30)
        status, reason, hdrs, body = resp.status, resp.reason, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        status, reason, hdrs, body = e.code, e.reason, e.headers, e.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"error connecting to {base}: {getattr(e, 'reason', e)}", file=sys.stderr)
        return 1
    out = sys.stdout.buffer
    out.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
    for k, v in hdrs.items():
        out.write(f"{k}: {v}\r\n".encode())
    out.write(b"\r\n" + body)
    out.flush()
    if 200 <= status < 300:
        return 0
    try:
        msg = json.loads(body).get("message", "")
    except (ValueError, AttributeError):
        msg = ""
    print(f"{tool}: {msg} (HTTP {status})", file=sys.stderr)
    return 1
