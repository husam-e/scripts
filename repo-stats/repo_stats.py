#!/usr/bin/env python3
"""repo-stats: a single-command code statistics dashboard for any git repository.

Reads a local git checkout (and, optionally, the GitHub or GitLab API) and writes
one self-contained HTML file with lines of code over time, PR/MR throughput, size,
type, risk and cycle-time statistics, and DORA metrics.

Python 3.8+, standard library only. Run `repo_stats.py --help` for usage.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import datetime as dt
import http.server
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

__version__ = "0.1.0"

TEMPLATE_PATH = Path(__file__).with_name("template.html")

# ---------------------------------------------------------------------------
# Heuristics. These are exported into the dashboard so the docs cannot drift.
# ---------------------------------------------------------------------------

CONVENTIONAL_TYPES = {
    "feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert",
}
TYPE_ALIASES = {
    "feature": "feat", "features": "feat", "bugfix": "fix", "hotfix": "fix", "bug": "fix",
    "doc": "docs", "tests": "test", "deps": "chore", "dep": "chore", "release": "chore",
    "refactoring": "refactor", "performance": "perf", "cleanup": "chore",
}
# Last-resort guesses from the first word of a title. Always labelled "keyword" in the output.
KEYWORD_TYPES = [
    (r"^(fix|bugfix|hotfix|patch|resolve|correct)\b", "fix"),
    (r"^(add|implement|introduce|support|create|enable)\b", "feat"),
    (r"^(bump|upgrade|update dep|chore|merge|release)\b", "chore"),
    (r"^(doc|docs|document|readme)\b", "docs"),
    (r"^(refactor|clean ?up|rename|move|simplify|extract)\b", "refactor"),
    (r"^(test|tests)\b", "test"),
    (r"^(revert)\b", "revert"),
    (r"^(perf|optimi[sz]e|speed)\b", "perf"),
]

RISK = {
    "size_lines": [[10, 0], [50, 5], [200, 12], [500, 22], [1000, 30], [None, 40]],
    "files": [[2, 0], [5, 4], [15, 8], [30, 12], [None, 15]],
    "type": {
        "feat": 8, "fix": 6, "refactor": 8, "perf": 8, "revert": 12, "build": 8, "ci": 8,
        "chore": 2, "style": 1, "test": 0, "docs": 0, "other": 5,
    },
    "breaking": 15,
    "sensitive_paths": 10,
    "no_tests": 8,
    "no_tests_min_code_lines": 50,
    "unreviewed": 10,
    "levels": [[20, "low"], [40, "medium"], [60, "high"], [None, "critical"]],
    "cap": 100,
}

SIZE_BUCKETS = [[10, "XS"], [100, "S"], [500, "M"], [1000, "L"], [None, "XL"]]

DEFAULT_SENSITIVE = (
    r"(^|/)(migrations?|db/migrate|schema|auth|authn|authz|security|crypto|secrets?|payments?|"
    r"billing|permissions?|iam|terraform|infra|deploy|k8s|helm)(/|\.|_|-|$)"
    r"|(^|/)\.github/workflows/|(^|/)(Dockerfile|docker-compose[^/]*\.ya?ml|\.gitlab-ci\.yml|Jenkinsfile)$"
    r"|\.(tf|tfvars)$"
)
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|specs?|testing)(/|$)|(_test|_spec|\.test|\.spec)\.[A-Za-z0-9]+$|(^|/)test_[^/]*$"
)
DOC_PATH = re.compile(r"\.(md|markdown|rst|adoc|txt)$|^docs?/", re.I)

DEFAULT_EXCLUDES = [
    r"(^|/)(node_modules|vendor|third_party|dist|build|\.venv|__pycache__)/",
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|Pipfile\.lock|"
    r"go\.sum|Gemfile\.lock|composer\.lock|uv\.lock|bun\.lockb?|flake\.lock)$",
    r"\.min\.(js|css)$",
    r"\.(map|snap|lock)$",
]

DEFAULT_FAILURE = r"hot-?fix|rollback|roll back|incident|regression|emergency"
DEFAULT_TAG_PATTERN = r"^v?\d+(\.\d+)*"

# file extension -> (language, kind). kind: code | data | docs | other
LANGUAGES = {
    ".py": ("Python", "code"), ".js": ("JavaScript", "code"), ".mjs": ("JavaScript", "code"),
    ".cjs": ("JavaScript", "code"), ".jsx": ("JavaScript", "code"), ".ts": ("TypeScript", "code"),
    ".tsx": ("TypeScript", "code"), ".go": ("Go", "code"), ".rs": ("Rust", "code"),
    ".java": ("Java", "code"), ".kt": ("Kotlin", "code"), ".kts": ("Kotlin", "code"),
    ".scala": ("Scala", "code"), ".swift": ("Swift", "code"), ".m": ("Objective-C", "code"),
    ".c": ("C", "code"), ".h": ("C/C++ header", "code"), ".cc": ("C++", "code"),
    ".cpp": ("C++", "code"), ".cxx": ("C++", "code"), ".hpp": ("C/C++ header", "code"),
    ".cs": ("C#", "code"), ".rb": ("Ruby", "code"), ".php": ("PHP", "code"), ".pl": ("Perl", "code"),
    ".lua": ("Lua", "code"), ".r": ("R", "code"), ".dart": ("Dart", "code"), ".ex": ("Elixir", "code"),
    ".exs": ("Elixir", "code"), ".erl": ("Erlang", "code"), ".hs": ("Haskell", "code"),
    ".clj": ("Clojure", "code"), ".ml": ("OCaml", "code"), ".zig": ("Zig", "code"),
    ".sh": ("Shell", "code"), ".bash": ("Shell", "code"), ".zsh": ("Shell", "code"),
    ".ps1": ("PowerShell", "code"), ".bat": ("Batch", "code"), ".sql": ("SQL", "code"),
    ".html": ("HTML", "code"), ".htm": ("HTML", "code"), ".css": ("CSS", "code"),
    ".scss": ("CSS", "code"), ".sass": ("CSS", "code"), ".less": ("CSS", "code"),
    ".vue": ("Vue", "code"), ".svelte": ("Svelte", "code"), ".tf": ("Terraform", "code"),
    ".proto": ("Protobuf", "code"), ".graphql": ("GraphQL", "code"), ".gradle": ("Gradle", "code"),
    ".json": ("JSON", "data"), ".yaml": ("YAML", "data"), ".yml": ("YAML", "data"),
    ".toml": ("TOML", "data"), ".xml": ("XML", "data"), ".csv": ("CSV", "data"),
    ".ini": ("Config", "data"), ".cfg": ("Config", "data"), ".conf": ("Config", "data"),
    ".md": ("Markdown", "docs"), ".markdown": ("Markdown", "docs"), ".rst": ("reStructuredText", "docs"),
    ".adoc": ("AsciiDoc", "docs"), ".txt": ("Text", "docs"),
}
SPECIAL_FILES = {
    "Dockerfile": ("Dockerfile", "code"), "Makefile": ("Makefile", "code"),
    "Jenkinsfile": ("Jenkinsfile", "code"), "CMakeLists.txt": ("CMake", "code"),
}
MAX_LANGS = 12


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class ToolError(Exception):
    """An error with a message that is safe and useful to show the user."""


def compile_re(pattern: str, what: str, flags: int = 0) -> re.Pattern:
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise ToolError(f"Invalid regular expression for {what}: {e}")


def tier(value, tiers):
    for limit, result in tiers:
        if limit is None or value <= limit:
            return result
    return tiers[-1][1]


def language_of(path: str):
    base = path.rsplit("/", 1)[-1]
    if base in SPECIAL_FILES:
        return SPECIAL_FILES[base]
    dot = base.rfind(".")
    if dot > 0:
        ext = base[dot:].lower()
        return LANGUAGES.get(ext, (ext, "other"))
    return ("(no extension)", "other")


# ---------------------------------------------------------------------------
# Git access
# ---------------------------------------------------------------------------

def git_cmd(repo, *args):
    return ["git", "-C", repo, "-c", "core.quotepath=off", "-c", "log.showSignature=false", *args]


def run_git(repo, *args, check=True) -> str:
    p = subprocess.run(git_cmd(repo, *args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise ToolError(f"git {' '.join(args[:3])} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def stream_git(repo, *args):
    """Yield stdout lines of a git command without loading it all in memory."""
    p = subprocess.Popen(git_cmd(repo, *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    assert p.stdout is not None
    for line in p.stdout:
        yield line
    p.wait()
    if p.returncode != 0:
        raise ToolError(f"git {' '.join(args[:3])} failed: {p.stderr.read().strip()}")


def detect_branch(repo: str) -> str:
    head = run_git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if head:
        return head
    for cand in ("origin/main", "origin/master", "main", "master"):
        if run_git(repo, "rev-parse", "--verify", "-q", cand + "^{commit}", check=False).strip():
            return cand
    return "HEAD"


def parse_remote(url: str):
    """Return (host, path) from an https, ssh or scp-style git remote URL, or None."""
    url = url.strip()
    m = re.match(r"^[\w.+-]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^(?:[^@/]+@)?([^/:]+):(.+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


def detect_provider(host: str):
    if host == "github.com" or host.startswith("github."):
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return None


# ---------------------------------------------------------------------------
# Conventional commits and message parsing
# ---------------------------------------------------------------------------

CC_RE = re.compile(r"^\s*(?:\[(?P<t2>[a-z]+)\]|(?P<t>[A-Za-z]+)(?:\((?P<scope>[^)\n]*)\))?(?P<bang>!)?)\s*:\s*(?P<desc>.*)$")


def normalize_type(raw: str):
    t = raw.lower()
    t = TYPE_ALIASES.get(t, t)
    return t if t in CONVENTIONAL_TYPES else None


def classify_type(title: str, branch: str | None = None, first_subject: str | None = None):
    """Return (type, scope, breaking, source). source says how the type was determined."""
    breaking = False
    for text, source in ((title, "title"), (first_subject, "commit")):
        if not text:
            continue
        m = CC_RE.match(text)
        if m:
            t = normalize_type(m.group("t") or m.group("t2") or "")
            if t:
                breaking = bool(m.group("bang")) or "BREAKING CHANGE" in text
                return t, m.group("scope") or None, breaking, source
    if branch:
        m = re.match(r"^(?:[\w.-]+/)?([A-Za-z]+)[/_-]", branch)
        if m:
            t = normalize_type(m.group(1))
            if t:
                return t, None, False, "branch"
    for text in (title, first_subject):
        if not text:
            continue
        low = text.strip().lower()
        for pattern, t in KEYWORD_TYPES:
            if re.match(pattern, low):
                return t, None, False, "keyword"
    return "other", None, False, "none"


def first_nonblank(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def parse_message(subject: str, body: str, default_branch: str):
    """Extract PR/MR number, title and branch from a commit on the default branch.

    Returns a dict with keys number, title, branch, sync (True for merges of the default
    branch into something else, which are not PRs), merge_style.
    """
    out = {"number": None, "title": None, "branch": None, "sync": False, "has_ref": False}
    short_default = default_branch.split("/", 1)[-1] if default_branch.startswith("origin/") else default_branch

    m = re.match(r"^Merge pull request #(\d+) from (\S+)", subject)
    if m:
        out.update(number=int(m.group(1)), branch=m.group(2).split("/", 1)[-1],
                   title=first_nonblank(body) or None, has_ref=True)
        return out
    m = re.match(r"^Merged in (\S+) \(pull request #(\d+)\)", subject)
    if m:
        out.update(number=int(m.group(2)), branch=m.group(1), title=first_nonblank(body) or None, has_ref=True)
        return out
    m = re.search(r"See merge request \S*?!(\d+)", body)
    if m:
        lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("See merge request")]
        bm = re.match(r"^Merge branch '([^']+)'", subject)
        out.update(number=int(m.group(1)), branch=bm.group(1) if bm else None,
                   title=(lines[0] if lines else subject), has_ref=True)
        if bm and not lines:
            out["title"] = None
        return out
    m = re.match(r"^(.*\S)\s+\(#(\d+)\)\s*$", subject)
    if m:
        out.update(number=int(m.group(2)), title=m.group(1), has_ref=True)
        return out
    m = re.match(r"^Merge (?:remote-tracking )?branch '([^']+)'(?: of \S+)?(?: into '?([^']+)'?)?", subject)
    if m:
        branch = re.sub(r"^origin/", "", m.group(1))
        out["branch"] = branch
        if branch == short_default:
            out["sync"] = True
        return out
    m = re.match(r"^Merge (?:tag|commit) '([^']+)'", subject)
    if m:
        out["branch"] = m.group(1)
    return out


def humanize_branch(branch: str | None):
    if not branch:
        return None
    name = re.sub(r"^(?:[\w.-]+/)?[A-Za-z]+[/_-]", "", branch, count=1) if re.match(r"^(?:[\w.-]+/)?[A-Za-z]+[/_-]", branch) else branch
    return re.sub(r"[-_]+", " ", name).strip() or branch


# ---------------------------------------------------------------------------
# Scanning history
# ---------------------------------------------------------------------------

class Scanner:
    """Walks the first-parent history of a branch and summarises each commit."""

    def __init__(self, excludes, sensitive_re):
        self.excludes = [compile_re(p, "--exclude") for p in excludes]
        self.sensitive = sensitive_re

    def summarize(self, numstat_lines):
        total_add = total_del = files = code_lines = 0
        tests = sensitive = False
        langs: dict = {}
        for line in numstat_lines:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) != 3 or parts[0] == "-":
                continue
            path = parts[2]
            if any(rx.search(path) for rx in self.excludes):
                continue
            try:
                a, d = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            total_add += a
            total_del += d
            files += 1
            lang, kind = language_of(path)
            la = langs.setdefault(lang, [0, 0, kind])
            la[0] += a
            la[1] += d
            is_test = bool(TEST_PATH.search(path))
            tests = tests or is_test
            if not is_test and not DOC_PATH.search(path) and kind == "code":
                code_lines += a + d
            if self.sensitive.search(path):
                sensitive = True
        return {"add": total_add, "del": total_del, "lines": total_add + total_del, "files": files,
                "code_lines": code_lines, "tests": tests, "sensitive": sensitive, "langs": langs}

    def scan(self, repo, branch):
        """Return a chronologically ordered list of first-parent commit dicts."""
        fmt = "%x1e%H%x1f%P%x1f%at%x1f%ct%x1f%an%x1f%s%x1f%b%x1f"
        args = ["log", "--first-parent", "-m", "--numstat", "--no-renames", "--reverse", f"--format={fmt}", branch]
        commits = []
        buf: list = []

        def flush():
            if not buf:
                return
            text = "".join(buf)
            fields = text.lstrip("\x1e").split("\x1f", 7)
            if len(fields) < 8:
                return
            sha, parents, at, ct, author, subject, body, tail = fields
            info = self.summarize(tail.split("\n"))
            info.update(sha=sha, parents=parents.split(), at=int(at), ct=int(ct), author=author,
                        subject=subject, body=body)
            commits.append(info)

        for line in stream_git(repo, *args):
            if line.startswith("\x1e"):
                flush()
                buf = [line]
            else:
                buf.append(line)
        flush()
        # -m on a merge prints the diff once per parent; --first-parent leaves one. Defensive dedupe.
        seen = set()
        unique = []
        for c in commits:
            if c["sha"] in seen:
                continue
            seen.add(c["sha"])
            unique.append(c)
        return unique


def load_graph(repo, branch):
    """sha -> (parents, author time, committer time) for every commit reachable from branch."""
    graph = {}
    for line in stream_git(repo, "log", "--format=%H %P|%at|%ct", branch):
        left, at, ct = line.rstrip("\n").split("|")
        parts = left.split()
        graph[parts[0]] = (tuple(parts[1:]), int(at), int(ct))
    return graph


def load_reverts(repo, branch):
    """sha -> sha (or prefix) of the commit it reverts, from 'This reverts commit' lines."""
    out = {}
    text = run_git(repo, "log", "--format=%x1e%H%x1f%b", "--grep=This reverts commit", branch, check=False)
    for rec in text.split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, body = rec.split("\x1f", 1)
        m = re.search(r"This reverts commit ([0-9a-f]{7,40})", body)
        if m:
            out[sha.strip()] = m.group(1)
    return out


def build_loc_series(first_parent):
    """Cumulative lines of code per UTC day, total and per language."""
    day = None
    days: list = []
    langs_cum: dict = collections.defaultdict(int)
    kinds: dict = {}
    snapshots: list = []  # per day: dict of cumulative lang -> lines
    total = 0
    last_ct = 0
    for c in first_parent:
        ct = max(c["ct"], last_ct)
        last_ct = ct
        d = ct - ct % 86400
        if d != day:
            days.append([d, 0, 0, 0])
            snapshots.append(None)
            day = d
        entry = days[-1]
        entry[2] += c["add"]
        entry[3] += c["del"]
        total += c["add"] - c["del"]
        for lang, (a, dl, kind) in c["langs"].items():
            langs_cum[lang] += a - dl
            kinds[lang] = kind
        entry[1] = total
        snapshots[-1] = dict(langs_cum)
    peak = collections.Counter()
    for snap in snapshots:
        for lang, v in snap.items():
            peak[lang] = max(peak[lang], v)
    top = [l for l, _ in peak.most_common(MAX_LANGS)]
    rest = [l for l in peak if l not in top]
    lang_series = {l: [max(0, s.get(l, 0)) for s in snapshots] for l in top}
    if rest:
        lang_series["Other"] = [max(0, sum(s.get(l, 0) for l in rest)) for s in snapshots]
        kinds["Other"] = "other"
    return {
        "days": days,
        "langs": lang_series,
        "kinds": {l: kinds.get(l, "other") for l in lang_series},
    }


def claim_branch_commits(first_parent, graph):
    """Map each merge commit on the first-parent line to the commits it brought in."""
    fp_set = {c["sha"] for c in first_parent}
    claimed: set = set()
    result = {}
    for c in first_parent:
        if len(c["parents"]) < 2:
            continue
        stack = [p for p in c["parents"][1:] if p not in fp_set and p not in claimed]
        found = []
        while stack:
            sha = stack.pop()
            if sha in claimed or sha in fp_set or sha not in graph:
                continue
            claimed.add(sha)
            found.append(sha)
            stack.extend(graph[sha][0])
        result[c["sha"]] = found
    return result


def compute_deploys(repo, graph, tag_pattern, branch):
    """Tags matching the pattern, with the first tag that contains each commit.

    Returns (deploys, first_tag_time) where first_tag_time maps commit sha -> tag time.
    """
    rx = compile_re(tag_pattern, "--tag-pattern")
    out = run_git(repo, "for-each-ref", "--format=%(refname:short)%09%(creatordate:unix)%09%(objectname)%09%(*objectname)",
                  "refs/tags", check=False)
    tags = []
    for line in out.splitlines():
        name, ts, obj, peeled = (line.split("\t") + ["", "", "", ""])[:4]
        sha = peeled or obj
        if not ts or not rx.search(name) or sha not in graph:
            continue
        tags.append((int(ts), name, sha))
    tags.sort()
    claimed: dict = {}
    deploys = []
    for ts, name, sha in tags:
        stack = [sha]
        members = []
        while stack:
            s = stack.pop()
            if s in claimed or s not in graph:
                continue
            claimed[s] = (ts, name)
            members.append(s)
            stack.extend(graph[s][0])
        deploys.append({"name": name, "t": ts, "sha": sha, "commits": len(members)})
    return deploys, claimed


# ---------------------------------------------------------------------------
# Inferring PRs from git alone
# ---------------------------------------------------------------------------

def infer_prs_from_git(first_parent, graph, default_branch, commits_as_prs=False):
    """Infer PRs from merge commits and squash commits that reference a PR number.

    Returns (prs, direct_commit_count, skipped_sync_merges).
    """
    claims = claim_branch_commits(first_parent, graph)
    prs = []
    direct = 0
    sync = 0
    for c in first_parent:
        msg = parse_message(c["subject"], c["body"], default_branch)
        is_merge = len(c["parents"]) >= 2
        if msg["sync"]:
            sync += 1
            continue
        if not is_merge and not msg["has_ref"]:
            if not commits_as_prs:
                direct += 1
                continue
        if is_merge and not msg["has_ref"] and msg["branch"] is None and c["subject"].startswith("Merge"):
            # a merge with no recognisable branch name; still a merge of something
            pass
        branch_shas = claims.get(c["sha"], []) if is_merge else []
        branch_commits = [graph[s] for s in branch_shas if len(graph[s][0]) < 2]
        pr = {
            "number": msg["number"],
            "sha": c["sha"],
            "title": msg["title"],
            "branch": msg["branch"],
            "state": "merged",
            "author_name": c["author"],
            "merged": c["ct"],
            "created": None,
            "ready": None,
            "first_review": None,
            "closed": c["ct"],
            "first_commit": None,
            "last_commit": None,
            "commits": None,
            "additions": c["add"],
            "deletions": c["del"],
            "files": c["files"],
            "labels": [],
            "reviewed": None,
            "local": c,
            "source": "merge-commit" if is_merge else "squash-commit",
            "first_subject": None,
            "inferred": ["created", "ready", "first_review"],
        }
        if is_merge and branch_commits:
            pr["first_commit"] = min(b[1] for b in branch_commits)
            pr["last_commit"] = max(b[1] for b in branch_commits)
            pr["commits"] = len(branch_commits)
            pr["branch_shas"] = branch_shas
        elif not is_merge:
            pr["first_commit"] = None
            pr["commits"] = None
        if pr["title"] is None:
            pr["title"] = humanize_branch(pr["branch"]) or c["subject"]
        prs.append(pr)
    return prs, direct, sync


# ---------------------------------------------------------------------------
# HTTP + API clients
# ---------------------------------------------------------------------------

class ApiError(ToolError):
    pass


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect to another host, so tokens cannot leak."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            raise ApiError(f"Refusing cross-host redirect to {urllib.parse.urlparse(newurl).netloc}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirect)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_json(url, headers, body=None, retries=4):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and not _is_loopback(parsed.hostname or ""):
        raise ApiError(f"Refusing to send credentials over insecure URL {parsed.scheme}://{parsed.netloc}")
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers)
    hdrs.setdefault("User-Agent", f"repo-stats/{__version__}")
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    delay = 2
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with _OPENER.open(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8")), resp.headers
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(e.headers.get("Retry-After", "0") or 0) or delay
                time.sleep(min(wait, 60))
                delay *= 2
                continue
            if e.code == 401:
                raise ApiError("API rejected the token (401). Check that it is valid and has read access.")
            if e.code == 403:
                raise ApiError("API returned 403: the token lacks access or the rate limit is exhausted.")
            if e.code == 404:
                raise ApiError("API returned 404: repository not found, or the token cannot see it.")
            raise ApiError(f"API request failed with HTTP {e.code}")
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise ApiError(f"Could not reach the API: {e.reason}")
    raise ApiError("API request failed")


def parse_iso(s):
    if not s:
        return None
    s = re.sub(r"\.\d+", "", s).replace("Z", "+0000")
    s = re.sub(r"([+-]\d\d):(\d\d)$", r"\1\2", s)
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp())


GITHUB_QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequests(first:50,after:$cursor,orderBy:{field:CREATED_AT,direction:DESC}){
      pageInfo{hasNextPage endCursor}
      nodes{
        number title state isDraft createdAt mergedAt closedAt additions deletions changedFiles
        headRefName baseRefName
        author{login __typename}
        labels(first:20){nodes{name}}
        mergeCommit{oid}
        commits(first:1){totalCount nodes{commit{authoredDate}}}
        reviews(first:20){nodes{submittedAt state author{login __typename}}}
        timelineItems(first:20,itemTypes:[READY_FOR_REVIEW_EVENT]){nodes{... on ReadyForReviewEvent{createdAt}}}
      }
    }
  }
}
"""


def fetch_github(owner, name, token, api_url, since, base_branch, all_bases, max_prs, progress=log):
    endpoint = api_url.rstrip("/") + "/graphql"
    headers = {"Authorization": f"Bearer {token}"}
    prs = []
    cursor = None
    while True:
        data, _ = http_json(endpoint, headers, {"query": GITHUB_QUERY,
                                                "variables": {"owner": owner, "name": name, "cursor": cursor}})
        if data.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in data["errors"])[:300]
            raise ApiError(f"GitHub GraphQL error: {msg}")
        page = data["data"]["repository"]["pullRequests"]
        stop = False
        for n in page["nodes"]:
            created = parse_iso(n["createdAt"])
            if since and created < since:
                stop = True
                continue
            if not all_bases and n.get("baseRefName") != base_branch:
                continue
            prs.append(github_pr(n))
            if max_prs and len(prs) >= max_prs:
                stop = True
                break
        progress(f"  fetched {len(prs)} pull requests")
        if stop or not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return prs


def _is_bot(login, typename=None):
    return bool(typename == "Bot" or (login and (login.endswith("[bot]") or login.endswith("-bot"))))


def github_pr(n):
    author = (n.get("author") or {})
    login = author.get("login")
    created = parse_iso(n["createdAt"])
    ready = created
    for t in (n.get("timelineItems") or {}).get("nodes", []):
        if t and t.get("createdAt"):
            ready = parse_iso(t["createdAt"])
            break
    reviews = [r for r in (n.get("reviews") or {}).get("nodes", [])
               if r and r.get("submittedAt") and r["state"] != "PENDING"
               and (r.get("author") or {}).get("login") != login
               and not _is_bot((r.get("author") or {}).get("login"), (r.get("author") or {}).get("__typename"))]
    first_review = min((parse_iso(r["submittedAt"]) for r in reviews), default=None)
    commits = n.get("commits") or {}
    first_commit = None
    if commits.get("nodes"):
        first_commit = parse_iso(commits["nodes"][0]["commit"]["authoredDate"])
    state = {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open"}.get(n["state"], "open")
    merge_commit = (n.get("mergeCommit") or {}).get("oid")
    return {
        "number": n["number"], "sha": merge_commit, "title": n["title"], "branch": n.get("headRefName"),
        "state": state, "author_name": login, "bot": _is_bot(login, author.get("__typename")),
        "created": created, "ready": ready, "first_review": first_review,
        "merged": parse_iso(n["mergedAt"]), "closed": parse_iso(n["closedAt"]),
        "first_commit": first_commit, "last_commit": None, "commits": commits.get("totalCount"),
        "additions": n.get("additions"), "deletions": n.get("deletions"), "files": n.get("changedFiles"),
        "labels": [l["name"] for l in (n.get("labels") or {}).get("nodes", []) if l],
        "reviewed": first_review is not None if state == "merged" else None,
        "source": "api", "inferred": [], "draft": n.get("isDraft"),
        "local": None, "first_subject": None,
    }


def fetch_gitlab(project_path, token, api_url, since, base_branch, all_bases, max_prs, want_reviews, progress=log):
    project = urllib.parse.quote(project_path, safe="")
    base = f"{api_url.rstrip('/')}/projects/{project}"
    headers = {"PRIVATE-TOKEN": token}
    mrs = []
    page = 1
    while True:
        q = {"state": "all", "scope": "all", "order_by": "created_at", "sort": "desc", "per_page": 100, "page": page}
        if since:
            q["created_after"] = dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not all_bases:
            q["target_branch"] = base_branch
        data, hdrs = http_json(f"{base}/merge_requests?{urllib.parse.urlencode(q)}", headers)
        mrs.extend(data)
        progress(f"  fetched {len(mrs)} merge requests")
        nxt = hdrs.get("X-Next-Page")
        if not data or not nxt or (max_prs and len(mrs) >= max_prs):
            break
        page = int(nxt)
    if max_prs:
        mrs = mrs[:max_prs]

    def details(mr):
        out = {"first_commit": None, "commits": None, "first_review": None}
        iid = mr["iid"]
        try:
            commits, _ = http_json(f"{base}/merge_requests/{iid}/commits?per_page=100", headers)
            times = [parse_iso(c.get("authored_date") or c.get("created_at")) for c in commits]
            times = [t for t in times if t]
            out["first_commit"] = min(times) if times else None
            out["commits"] = len(commits)
            if want_reviews:
                notes, _ = http_json(f"{base}/merge_requests/{iid}/notes?sort=asc&order_by=created_at&per_page=100", headers)
                me = (mr.get("author") or {}).get("username")
                for note in notes:
                    a = note.get("author") or {}
                    if note.get("system") or a.get("username") == me or _is_bot(a.get("username"), "Bot" if a.get("bot") else None):
                        continue
                    out["first_review"] = parse_iso(note["created_at"])
                    break
        except ApiError as e:
            out["error"] = str(e)
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        extra = list(pool.map(details, mrs))
    result = []
    for mr, ex in zip(mrs, extra):
        author = mr.get("author") or {}
        state = {"merged": "merged", "closed": "closed", "opened": "open", "locked": "open"}.get(mr["state"], "open")
        created = parse_iso(mr["created_at"])
        sha = mr.get("merge_commit_sha") or mr.get("squash_commit_sha")
        result.append({
            "number": mr["iid"], "sha": sha, "title": mr["title"], "branch": mr.get("source_branch"),
            "state": state, "author_name": author.get("username"), "bot": _is_bot(author.get("username"), "Bot" if author.get("bot") else None),
            "created": created, "ready": created, "first_review": ex["first_review"],
            "merged": parse_iso(mr.get("merged_at")), "closed": parse_iso(mr.get("closed_at") or mr.get("merged_at")),
            "first_commit": ex["first_commit"], "last_commit": None, "commits": ex["commits"],
            "additions": None, "deletions": None, "files": None,
            "labels": mr.get("labels") or [],
            "reviewed": (ex["first_review"] is not None) if (state == "merged" and want_reviews) else None,
            "source": "api", "inferred": ["ready"] + ([] if want_reviews else ["first_review"]),
            "squash_sha": mr.get("squash_commit_sha"), "local": None, "first_subject": None,
            "draft": mr.get("draft") or mr.get("work_in_progress"),
        })
    return result


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def risk_for(pr):
    """Score a PR 0..100 using the additive heuristic in RISK. Returns (score, level, factors)."""
    factors = []

    def add(name, pts, detail):
        if pts:
            factors.append({"name": name, "points": pts, "detail": detail})

    lines, files = pr.get("lines"), pr.get("files")
    if lines is not None:
        add("size", tier(lines, RISK["size_lines"]), f"{lines} lines changed")
    if files is not None:
        add("files", tier(files, RISK["files"]), f"{files} files")
    add("type", RISK["type"].get(pr["type"], RISK["type"]["other"]), f"type {pr['type']}")
    if pr["breaking"]:
        add("breaking", RISK["breaking"], "breaking change marker")
    if pr.get("sensitive"):
        add("sensitive paths", RISK["sensitive_paths"], "touches migrations, auth, infra or CI paths")
    if pr.get("tests") is False and (pr.get("code_lines") or 0) >= RISK["no_tests_min_code_lines"]:
        add("no tests", RISK["no_tests"], f"{pr['code_lines']} code lines, no test files")
    if pr.get("reviewed") is False:
        add("unreviewed", RISK["unreviewed"], "merged with no review from another person")
    score = min(RISK["cap"], sum(f["points"] for f in factors))
    level = tier(score, [[limit - 1 if limit else None, name] for limit, name in RISK["levels"]])
    return score, level, factors


def local_summary(pr, fp_by_sha):
    """Return the first-parent commit summary that represents this PR's full diff, if trustworthy."""
    if pr.get("local"):
        return pr["local"]
    c = fp_by_sha.get(pr.get("sha") or "")
    if not c:
        return None
    if len(c["parents"]) >= 2 or (pr.get("squash_sha") and pr["squash_sha"] == c["sha"]):
        return c
    if pr["number"] is not None and re.search(rf"\(#{pr['number']}\)\s*$", c["subject"]):
        return c
    return None


def build_records(prs, ctx):
    """Turn raw PR dicts into the final records embedded in the dashboard."""
    failure_re = ctx["failure_re"]
    fp_by_sha = ctx["fp_by_sha"]
    claimed = ctx["claimed"]
    records = []
    sha_to_pr: dict = {}
    for pr in prs:
        c = local_summary(pr, fp_by_sha)
        if c is not None and pr["source"] == "api":
            if pr["additions"] is None:  # GitLab: size comes from the local commit
                pr["additions"], pr["deletions"], pr["files"] = c["add"], c["del"], c["files"]
        first_subject = pr.get("first_subject")
        t, scope, breaking, tsrc = classify_type(pr["title"] or "", pr.get("branch"), first_subject)
        if t == "other":
            for label in pr.get("labels") or []:
                lt = normalize_type(label.split(":")[-1].strip())
                if lt:
                    t, tsrc = lt, "label"
                    break
        text_for_failure = " ".join(filter(None, [pr["title"], pr.get("branch"), " ".join(pr.get("labels") or [])]))
        kind = None
        if t == "revert" or (pr["title"] or "").lower().startswith("revert"):
            kind = "revert"
        elif failure_re.search(text_for_failure):
            kind = "hotfix"
        add_, del_ = pr["additions"], pr["deletions"]
        lines = (add_ + del_) if add_ is not None and del_ is not None else None
        rec = {
            "id": pr["number"] if pr["number"] is not None else pr["sha"][:8],
            "number": pr["number"],
            "title": pr["title"] if not ctx["redact"] else (f"#{pr['number']}" if pr["number"] is not None else pr["sha"][:8]),
            "type": t, "scope": scope, "breaking": breaking, "type_source": tsrc,
            "state": pr["state"], "bot": pr.get("bot") or bool(ctx["bot_re"].search(pr.get("author_name") or "")),
            "created": pr["created"], "first_commit": pr["first_commit"], "ready": pr["ready"],
            "first_review": pr["first_review"], "merged": pr["merged"], "closed": pr["closed"],
            "additions": add_, "deletions": del_, "lines": lines, "files": pr["files"], "commits": pr["commits"],
            "kind": kind, "source": pr["source"], "inferred": list(pr["inferred"]),
            "reviewed": pr.get("reviewed"),
        }
        sensitive = tests = code_lines = None
        if c is not None:
            sensitive, tests, code_lines = c["sensitive"], c["tests"], c["code_lines"]
        rec.update(sensitive=sensitive, tests=tests, code_lines=code_lines)
        score, level, factors = risk_for({**rec, "lines": lines})
        rec["risk"], rec["risk_level"] = score, level
        rec["risk_factors"] = [[f["name"], f["points"]] for f in factors]
        # Durations in seconds. None means "not available for this record".
        def span(a, b):
            return b - a if a is not None and b is not None and b >= a else None

        merged = pr["merged"] if pr["state"] == "merged" else None
        if pr["source"] == "api":
            rec["coding"] = span(pr["first_commit"], pr["ready"])
            rec["pickup"] = span(pr["ready"], pr["first_review"])
            rec["review"] = span(pr["first_review"], merged)
            rec["open_to_merge"] = span(pr["created"], merged)
        else:
            rec["coding"] = span(pr["first_commit"], pr["last_commit"])
            rec["pickup"] = None
            rec["review"] = span(pr["last_commit"], merged)
            rec["open_to_merge"] = None
            if rec["coding"] is not None:
                rec["inferred"].append("coding")
            if rec["review"] is not None:
                rec["inferred"].append("review")
        rec["lead"] = span(pr["first_commit"], merged)
        if rec["lead"] is not None and pr["source"] != "api":
            rec["inferred"].append("lead")
        deploy = claimed.get(pr.get("sha") or "")
        rec["deployed"] = deploy[0] if deploy and merged is not None else None
        rec["release"] = deploy[1] if deploy and merged is not None else None
        rec["deploy_wait"] = span(merged, rec["deployed"])
        rec["lead_to_deploy"] = span(pr["first_commit"], rec["deployed"])
        rec["restore"] = None
        rec["_sha"] = pr.get("sha")
        rec["_c"] = c
        records.append(rec)
        if merged is not None:
            for s in {pr.get("sha"), pr.get("squash_sha")} - {None}:
                sha_to_pr[s] = rec
            for s in pr.get("branch_shas", []):
                sha_to_pr[s] = rec
    # Time to restore: reverts that name the reverted commit.
    reverts = ctx["reverts"]
    for rec, pr in zip(records, prs):
        if rec["kind"] != "revert" or rec["merged"] is None:
            continue
        shas = [pr.get("sha"), pr.get("squash_sha")] + list(pr.get("branch_shas", []))
        for sha in shas:
            prefix = reverts.get(sha or "")
            if not prefix:
                continue
            for other, orig in sha_to_pr.items():
                if other.startswith(prefix) and orig is not rec and orig["merged"] is not None:
                    if rec["merged"] >= orig["merged"]:
                        rec["restore"] = rec["merged"] - orig["merged"]
                    break
            if rec["restore"] is not None:
                break
    for rec in records:
        rec.pop("_sha")
        rec.pop("_c")
    return records


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def embed_json(obj) -> str:
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
               .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_html(data) -> str:
    if not TEMPLATE_PATH.exists():
        raise ToolError(f"Missing {TEMPLATE_PATH.name} next to repo_stats.py")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    marker = "/*__DATA__*/null"
    if marker not in template:
        raise ToolError("template.html is missing the data marker")
    return template.replace(marker, embed_json(data), 1)


def write_csv(path, records):
    cols = ["id", "title", "type", "scope", "breaking", "state", "bot", "created", "first_commit", "first_review",
            "merged", "additions", "deletions", "files", "commits", "risk", "risk_level", "kind",
            "coding", "pickup", "review", "open_to_merge", "lead", "deployed", "deploy_wait", "lead_to_deploy",
            "restore", "source"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in records:
            row = []
            for c in cols:
                v = r.get(c)
                if isinstance(v, str) and v[:1] in "=+-@":
                    v = "'" + v  # neutralise spreadsheet formula injection
                row.append("" if v is None else v)
            w.writerow(row)


def resolve_token(env_names):
    for name in env_names:
        v = os.environ.get(name)
        if v:
            return v.strip(), name
    return None, None


def parse_date(s):
    if not s:
        return None
    try:
        return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
    except ValueError:
        raise ToolError(f"Invalid date '{s}'. Use YYYY-MM-DD.")


def collect(args):
    repo = os.path.abspath(args.repo)
    if run_git(repo, "rev-parse", "--is-inside-work-tree", check=False).strip() != "true" \
            and run_git(repo, "rev-parse", "--is-bare-repository", check=False).strip() != "true":
        raise ToolError(f"{repo} is not a git repository")
    branch = args.branch or detect_branch(repo)
    if not run_git(repo, "rev-parse", "--verify", "-q", branch + "^{commit}", check=False).strip():
        raise ToolError(f"Branch or ref '{branch}' not found. Pass --branch.")
    short_branch = branch.split("/", 1)[1] if branch.startswith("origin/") else branch
    warnings: list = []
    if run_git(repo, "rev-parse", "--is-shallow-repository", check=False).strip() == "true":
        warnings.append("This is a shallow clone. History, lines of code over time and timings are incomplete. "
                        "Run `git fetch --unshallow` for full results.")
    since = parse_date(args.since)
    until = parse_date(args.until)

    excludes = [] if args.no_default_excludes else list(DEFAULT_EXCLUDES)
    excludes += args.exclude or []
    sensitive_re = compile_re(args.sensitive_pattern or DEFAULT_SENSITIVE, "--sensitive-pattern", re.I)
    failure_re = compile_re(args.failure_pattern or DEFAULT_FAILURE, "--failure-pattern", re.I)
    bot_re = compile_re(args.bot_pattern, "--bot-pattern", re.I)

    log(f"Scanning history of {branch} ...")
    scanner = Scanner(excludes, sensitive_re)
    first_parent = scanner.scan(repo, branch)
    if not first_parent:
        raise ToolError(f"No commits found on {branch}")
    graph = load_graph(repo, branch)
    fp_by_sha = {c["sha"]: c for c in first_parent}
    loc = build_loc_series(first_parent)
    log(f"  {len(graph)} commits, {len(first_parent)} on the first-parent line")

    deploys, claimed = ([], {})
    if args.deploy_source == "tags":
        deploys, claimed = compute_deploys(repo, graph, args.tag_pattern, branch)
        log(f"  {len(deploys)} release tags")
    else:
        for c in first_parent:
            claimed[c["sha"]] = (c["ct"], "merge")
        deploys = []

    # Decide where PR data comes from.
    remote = run_git(repo, "remote", "get-url", args.remote, check=False).strip()
    parsed = parse_remote(remote) if remote else None
    provider = args.provider or (detect_provider(parsed[0]) if parsed else None)
    token_envs = [args.token_env] if args.token_env else (
        ["GITHUB_TOKEN", "GH_TOKEN"] if provider == "github" else
        ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"] if provider == "gitlab" else [])
    token, token_name = resolve_token(token_envs)

    source = args.source
    api_prs = None
    if source in ("auto", "api"):
        if not parsed or not provider:
            msg = "No GitHub or GitLab remote was detected (use --remote or --provider)."
            if source == "api":
                raise ToolError(msg)
            warnings.append(msg + " PRs are inferred from git history.")
        elif not token:
            msg = (f"No API token found (looked for {', '.join(token_envs) or 'a token variable'}).")
            if source == "api":
                raise ToolError(msg)
            warnings.append(msg + " Using PRs inferred from git history. Set a token for exact open, review and merge times.")
        else:
            host, path = parsed
            try:
                log(f"Fetching pull requests from {provider} API (token from ${token_name}) ...")
                if provider == "github":
                    owner, name = path.split("/", 1)
                    api_url = args.api_url or ("https://api.github.com" if host == "github.com" else f"https://{host}/api")
                    api_prs = fetch_github(owner, name, token, api_url, since, short_branch, args.all_bases, args.max_prs)
                else:
                    api_url = args.api_url or f"https://{host}/api/v4"
                    api_prs = fetch_gitlab(path, token, api_url, since, short_branch, args.all_bases, args.max_prs,
                                           not args.no_reviews)
            except ApiError as e:
                if source == "api":
                    raise
                warnings.append(f"API fetch failed ({e}). Using PRs inferred from git history instead.")
                api_prs = None

    direct = sync = 0
    if api_prs is not None:
        prs, mode = api_prs, "api"
    else:
        prs, direct, sync = infer_prs_from_git(first_parent, graph, branch, args.commits_as_prs)
        mode = "git"
        if not prs:
            warnings.append("No pull requests could be inferred from git history. This usually means rebase or "
                            "fast-forward merges with no PR number in the commit message. Use the API (set a token) "
                            "or --commits-as-prs.")
        if deploys == [] and args.deploy_source == "tags":
            warnings.append("No release tags matched, so deployment frequency and lead time to deploy are empty. "
                            "Adjust --tag-pattern or use --deploy-source merges.")

    if since:
        prs = [p for p in prs if (p["merged"] or p["created"] or 0) >= since]
    if until:
        prs = [p for p in prs if (p["merged"] or p["created"] or 0) <= until + 86400]
    if args.max_prs and mode == "git":
        prs = prs[-args.max_prs:]

    records = build_records(prs, {
        "failure_re": failure_re, "bot_re": bot_re, "fp_by_sha": fp_by_sha, "claimed": claimed,
        "redact": args.no_titles, "reverts": load_reverts(repo, branch),
    })
    if args.exclude_bots:
        records = [r for r in records if not r["bot"]]
    records.sort(key=lambda r: r["merged"] or r["created"] or 0)

    if since:
        deploys = [d for d in deploys if d["t"] >= since]
    if until:
        deploys = [d for d in deploys if d["t"] <= until + 86400]
    # Mark deploy as failure-related when it contains a revert/hotfix PR.
    fail_releases = {r["release"] for r in records if r["kind"] and r["release"]}
    for d in deploys:
        d["fix"] = d["name"] in fail_releases

    name = args.title or (parsed[1].rsplit("/", 1)[-1] if parsed else os.path.basename(repo))
    meta = {
        "name": name, "branch": branch, "generated": int(time.time()), "version": __version__,
        "mode": mode, "provider": provider if mode == "api" else None,
        "warnings": warnings, "commits": len(graph), "direct_commits": direct, "sync_merges": sync,
        "deploy_source": args.deploy_source, "tag_pattern": args.tag_pattern,
        "failure_pattern": args.failure_pattern or DEFAULT_FAILURE, "excludes": excludes,
        "reviews": mode == "api" and not (provider == "gitlab" and args.no_reviews),
        "commits_as_prs": bool(args.commits_as_prs),
        "heuristics": {"risk": RISK, "size_buckets": SIZE_BUCKETS, "sensitive": args.sensitive_pattern or DEFAULT_SENSITIVE,
                       "test_path": TEST_PATH.pattern, "doc_path": DOC_PATH.pattern},
    }
    return {"meta": meta, "prs": records, "loc": loc, "deploys": deploys}


def build_parser():
    p = argparse.ArgumentParser(
        prog="repo_stats.py",
        description="Generate a self-contained HTML dashboard of code statistics for a git repository.",
        epilog="Environment: GITHUB_TOKEN / GH_TOKEN or GITLAB_TOKEN / GITLAB_PRIVATE_TOKEN enable exact PR data. "
               "Tokens are read from the environment only and are never written to the output.",
    )
    p.add_argument("repo", nargs="?", default=".", help="path to a local git checkout (default: .)")
    p.add_argument("-o", "--out", default="repo-stats.html", help="output HTML file (default: repo-stats.html)")
    p.add_argument("--json", metavar="FILE", help="also write the raw dashboard data as JSON")
    p.add_argument("--csv", metavar="FILE", help="also write one row per PR as CSV")
    p.add_argument("--title", help="name shown in the dashboard (default: repository name)")
    p.add_argument("--branch", help="branch to analyse (default: the remote's default branch)")
    p.add_argument("--remote", default="origin", help="remote used to find the hosting provider (default: origin)")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only include PRs on or after this date")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only include PRs on or before this date")
    g = p.add_argument_group("PR data source")
    g.add_argument("--source", choices=["auto", "git", "api"], default="auto",
                   help="auto uses the API when a token is available, otherwise infers PRs from git (default: auto)")
    g.add_argument("--provider", choices=["github", "gitlab"], help="hosting provider (default: detected from the remote)")
    g.add_argument("--api-url", help="API base URL for self-hosted servers, e.g. https://host/api/v4 (GitLab) or https://host/api (GitHub Enterprise)")
    g.add_argument("--token-env", metavar="NAME", help="environment variable that holds the API token")
    g.add_argument("--no-reviews", action="store_true", help="GitLab: skip per-MR review lookups (faster, fewer API calls)")
    g.add_argument("--all-bases", action="store_true", help="include PRs that target branches other than the analysed one")
    g.add_argument("--max-prs", type=int, metavar="N", help="stop after N PRs (newest first)")
    g.add_argument("--commits-as-prs", action="store_true",
                   help="git mode: treat every direct commit on the branch as a PR (for repos without PR references)")
    g = p.add_argument_group("heuristics")
    g.add_argument("--exclude", action="append", metavar="REGEX", help="extra path regex to ignore for lines of code and size (repeatable)")
    g.add_argument("--no-default-excludes", action="store_true", help="count lockfiles, vendored and generated files too")
    g.add_argument("--deploy-source", choices=["tags", "merges"], default="tags",
                   help="what counts as a deployment: release tags, or every merge to the branch (default: tags)")
    g.add_argument("--tag-pattern", default=DEFAULT_TAG_PATTERN, metavar="REGEX", help=f"tags that count as releases (default: {DEFAULT_TAG_PATTERN})")
    g.add_argument("--failure-pattern", metavar="REGEX", help=f"title/branch/label regex marking a hotfix (default: {DEFAULT_FAILURE})")
    g.add_argument("--sensitive-pattern", metavar="REGEX", help="path regex that raises risk (default: migrations, auth, infra, CI)")
    g.add_argument("--bot-pattern", default=r"\[bot\]$|^dependabot|^renovate|-bot$", metavar="REGEX", help="author regex that marks a bot")
    g.add_argument("--exclude-bots", action="store_true", help="drop bot-authored PRs entirely")
    g = p.add_argument_group("privacy and viewing")
    g.add_argument("--no-titles", action="store_true", help="replace PR titles with their number in the output")
    g.add_argument("--serve", nargs="?", const=8000, type=int, metavar="PORT", help="serve the result on localhost and open a browser")
    g.add_argument("--no-open", action="store_true", help="with --serve, do not open a browser")
    p.add_argument("--version", action="version", version=f"repo-stats {__version__}")
    return p


def serve(path: Path, port: int, open_browser: bool):
    directory = str(path.parent.resolve())

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{path.name}"
    log(f"Serving {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        data = collect(args)
        out = Path(args.out)
        out.write_text(render_html(data), encoding="utf-8")
        if args.json:
            Path(args.json).write_text(json.dumps(data, indent=1), encoding="utf-8")
        if args.csv:
            write_csv(args.csv, data["prs"])
    except ToolError as e:
        log(f"error: {e}")
        return 1
    merged = sum(1 for r in data["prs"] if r["state"] == "merged")
    log(f"Wrote {out} ({len(data['prs'])} PRs, {merged} merged, source: {data['meta']['mode']})")
    for w in data["meta"]["warnings"]:
        log(f"warning: {w}")
    if args.serve is not None:
        serve(out, args.serve, not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
