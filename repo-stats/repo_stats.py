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
import email.utils
import http.client
import http.server
import ipaddress
import json
import os
import re
import subprocess
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import Message
from pathlib import Path
from typing import IO, Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple, TypedDict, cast

__version__ = "0.1.0"

# A first-parent commit summary from Scanner.scan: sha, parents, at, ct, author, subject, body, add, del,
# lines, files, code_lines, tests, sensitive, langs.
Commit = Dict[str, Any]
# A raw PR/MR from the API or inferred from git, before build_records.
PR = Dict[str, Any]
# A final per-PR record embedded in the dashboard.
Record = Dict[str, Any]
# sha -> (parent shas, author time, committer time)
Graph = Dict[str, Tuple[Tuple[str, ...], int, int]]
# sha -> (release time, release name) of the first deployment that contains the commit
DeployClaims = Dict[str, Tuple[int, str]]
# [[upper limit or None, result], ...] as used by tier()
Tiers = Sequence[Sequence[Any]]


class ParsedMessage(TypedDict):
    """PR reference details extracted from a commit message by parse_message."""

    number: Optional[int]
    title: Optional[str]
    branch: Optional[str]
    sync: bool
    has_ref: bool


class RiskFactor(TypedDict):
    """One contribution to a PR's risk score."""

    name: str
    points: int
    detail: str

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

# Matched anywhere in the branch name and labels, but only at the start of the title (after an optional
# Conventional Commit prefix), so "feat: add rollback support" is not a failure.
DEFAULT_FAILURE = r"\bhot-?fix\b|\broll-? ?back\b|\bemergency\b"
# Dotted version tags (v1.2, 1.2.3, 2024.05.01). Pre-releases are not deployments: -rc1, -beta, .alpha2,
# 1.0.0dev, -pre, -preview, -next, -canary, -SNAPSHOT, -nightly and PEP 440 1.0.0a1 / 1.0.0b2.
DEFAULT_TAG_PATTERN = (r"^(?!.*(?:[-._+]|\d)(?i:rc|alpha|beta|dev|pre|preview|next|canary|snapshot|nightly)(?![A-Za-z]))"
                       r"(?!.*\d[ab]\d)v?\d+(\.\d+)+")

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
    """Print a progress or error message to stderr."""
    print(msg, file=sys.stderr, flush=True)


class ToolError(Exception):
    """An error with a message that is safe and useful to show the user."""


def compile_re(pattern: str, what: str, flags: int = 0) -> re.Pattern[str]:
    """Compile a user-supplied regex, raising ToolError that names the option `what` on failure."""
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise ToolError(f"Invalid regular expression for {what}: {e}")


def tier(value: float, tiers: Tiers) -> Any:
    """Return the result of the first tier whose limit is None or at least `value`."""
    for limit, result in tiers:
        if limit is None or value <= limit:
            return result
    return tiers[-1][1]


def language_of(path: str) -> Tuple[str, str]:
    """Return (language, kind) for a file path; kind is code, data, docs or other."""
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

def git_cmd(repo: str, *args: str) -> List[str]:
    """Build a git argv for `repo` with stable, unquoted path output."""
    return ["git", "-C", repo, "-c", "core.quotepath=off", "-c", "log.showSignature=false", *args]


def run_git(repo: str, *args: str, check: bool = True) -> str:
    """Run git and return its stdout; with check, raise ToolError on a non-zero exit."""
    p = subprocess.run(git_cmd(repo, *args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise ToolError(f"git {' '.join(args[:3])} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def stream_git(repo: str, *args: str) -> Iterator[str]:
    """Yield stdout lines of a git command without loading it all in memory."""
    # stderr goes to a temp file: a PIPE nobody reads can fill up and deadlock git.
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(git_cmd(repo, *args), stdout=subprocess.PIPE, stderr=err,
                             text=True, encoding="utf-8", errors="replace")
        assert p.stdout is not None
        with p.stdout:
            for line in p.stdout:
                yield line
        p.wait()
        if p.returncode != 0:
            err.seek(0)
            msg = err.read().decode("utf-8", "replace").strip()
            raise ToolError(f"git {' '.join(args[:3])} failed: {msg}")


def detect_branch(repo: str, remote: str = "origin") -> str:
    """Guess the default branch: the remote's HEAD, then main/master, then the checked-out branch."""
    head = run_git(repo, "symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD", check=False).strip()
    if head:
        return head
    for cand in (f"{remote}/main", f"{remote}/master", "main", "master"):
        if run_git(repo, "rev-parse", "--verify", "-q", cand + "^{commit}", check=False).strip():
            return cand
    # Fall back to the checked-out branch (e.g. "trunk"), or HEAD when detached.
    return run_git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip() or "HEAD"


def strip_remote_prefix(repo: str, branch: str) -> str:
    """'upstream/main' -> 'main' when it names a remote-tracking ref (and no local branch of that name)."""
    if branch.startswith("refs/remotes/"):
        branch = branch[len("refs/remotes/"):]
    elif branch.startswith("refs/heads/"):
        return branch[len("refs/heads/"):]
    if "/" not in branch:
        return branch
    if run_git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).strip():
        return branch
    if run_git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{branch}", check=False).strip():
        return branch.split("/", 1)[1]
    return branch


def parse_remote(url: str) -> Optional[Tuple[str, str]]:
    """Return (host, path) from an https, ssh or scp-style git remote URL, or None."""
    url = url.strip()
    m = re.match(r"^[\w.+-]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^(?:[^@/]+@)?([^/:]+):(.+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


TRUSTED_HOSTS = {"github.com": "github", "gitlab.com": "gitlab"}


def detect_provider(host: str) -> Optional[str]:
    """Return "github" or "gitlab" for the public hosts only, else None.

    Anything else needs --provider and --api-url, so a crafted remote URL (github.evil.example) can never
    receive a token.
    """
    return TRUSTED_HOSTS.get(host)


# ---------------------------------------------------------------------------
# Conventional commits and message parsing
# ---------------------------------------------------------------------------

CC_RE = re.compile(r"^\s*(?:\[(?P<t2>[a-z]+)\]|(?P<t>[A-Za-z]+)(?:\((?P<scope>[^)\n]*)\))?(?P<bang>!)?)\s*:\s*(?P<desc>.*)$")


def normalize_type(raw: str) -> Optional[str]:
    """Map a type word or alias to a Conventional Commit type, or None if it is not one."""
    t = raw.lower()
    t = TYPE_ALIASES.get(t, t)
    return t if t in CONVENTIONAL_TYPES else None


def classify_type(title: str, branch: str | None = None,
                  labels: Optional[Sequence[str]] = None) -> Tuple[str, Optional[str], bool, str]:
    """Return (type, scope, breaking, source).

    The order is: Conventional Commit title, branch prefix, a type label, then a keyword guess from the title.
    source says which one decided: title, branch, label, keyword or none.
    """
    m = CC_RE.match(title) if title else None
    if m:
        t = normalize_type(m.group("t") or m.group("t2") or "")
        if t:
            breaking = bool(m.group("bang")) or "BREAKING CHANGE" in title
            return t, m.group("scope") or None, breaking, "title"
    if branch:
        m = re.match(r"^(?:[\w.-]+/)?([A-Za-z]+)[/_-]", branch)
        if m:
            t = normalize_type(m.group(1))
            if t:
                return t, None, False, "branch"
    for label in labels or []:
        t = normalize_type(label.split(":")[-1].strip())
        if t:
            return t, None, False, "label"
    low = (title or "").strip().lower()
    for pattern, t in KEYWORD_TYPES:
        if re.match(pattern, low):
            return t, None, False, "keyword"
    return "other", None, False, "none"


def first_nonblank(text: str) -> str:
    """Return the first non-blank line of text, stripped, or ""."""
    # split on "\n" only: str.splitlines() also breaks on U+2028/U+2029/U+0085 inside a title
    for line in text.split("\n"):
        if line.strip():
            return line.strip()
    return ""


def parse_message(subject: str, body: str, default_branch: str) -> ParsedMessage:
    """Extract PR/MR number, title and branch from a commit on the default branch.

    sync is True for merges of the default branch into something else, which are not PRs; has_ref is
    True when the message references a PR/MR number.
    """
    out: ParsedMessage = {"number": None, "title": None, "branch": None, "sync": False, "has_ref": False}
    short_default = default_branch[len("origin/"):] if default_branch.startswith("origin/") else default_branch

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
        lines = [ln.strip() for ln in body.split("\n") if ln.strip() and not ln.strip().startswith("See merge request")]
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
        branch = m.group(1)
        if subject.startswith("Merge remote-tracking branch ") and "/" in branch:
            branch = branch.split("/", 1)[1]  # any remote name, not only origin
        else:
            branch = re.sub(r"^origin/", "", branch)
        out["branch"] = branch
        if branch == short_default:
            out["sync"] = True
        return out
    m = re.match(r"^Merge (?:tag|commit) '([^']+)'", subject)
    if m:
        out["branch"] = m.group(1)
    return out


def humanize_branch(branch: str | None) -> Optional[str]:
    """Turn a branch name into a readable title ("feat/login-page" -> "login page")."""
    if not branch:
        return None
    if re.match(r"^(dependabot|renovate)/", branch):
        # dependabot/npm_and_yarn/lodash-4.1 -> "lodash 4.1"
        return re.sub(r"[-_]+", " ", branch.rsplit("/", 1)[-1]).strip() or branch
    name = branch.rsplit("/", 1)[-1]  # feat/login-page, dev/feat/x -> last segment
    if "/" not in branch:
        m = re.match(r"^([A-Za-z]+)[_-](.+)$", branch)  # fix-typo -> typo
        if m and normalize_type(m.group(1)) not in (None, "revert"):
            name = m.group(2)
    return re.sub(r"[-_]+", " ", name).strip() or branch


# ---------------------------------------------------------------------------
# Scanning history
# ---------------------------------------------------------------------------

class Scanner:
    """Walks the first-parent history of a branch and summarises each commit."""

    def __init__(self, excludes: Iterable[str], sensitive_re: re.Pattern[str]) -> None:
        """Set up path exclusion regexes and the sensitive-path regex."""
        self.excludes = [compile_re(p, "--exclude") for p in excludes]
        self.sensitive = sensitive_re

    def summarize(self, numstat_lines: Iterable[str]) -> Commit:
        """Sum `git log --numstat` lines into size, file, language, test and sensitive-path stats.

        langs maps language -> [added, deleted, kind].
        """
        total_add = total_del = files = code_lines = 0
        tests = sensitive = False
        langs: dict[str, list[Any]] = {}
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

    def scan(self, repo: str, branch: str) -> List[Commit]:
        """Return a chronologically ordered list of first-parent commit dicts.

        Commit messages are attacker-controlled text (on GitHub a squash body is the PR description) and git
        does not escape control characters in them. So metadata and diff stats come from two separate git
        commands: the metadata one is NUL-delimited (-z), and a message can never contain NUL; the numstat one
        prints no message text at all.
        """
        meta_fmt = "%H%x00%P%x00%at%x00%ct%x00%an%x00%s%x00%b"
        commits: list[Commit] = []
        by_sha: dict[str, Commit] = {}
        fields = run_git(repo, "log", "-z", "--first-parent", "--reverse", f"--format={meta_fmt}", branch).split("\0")
        n = 7
        for i in range(0, len(fields) - n + 1, n):
            sha, parents, at, ct, author, subject, body = fields[i:i + n]
            sha = sha.lstrip("\n")
            if sha in by_sha:  # guard against double counting
                continue
            c: Commit = {"sha": sha, "parents": parents.split(), "at": int(at), "ct": int(ct), "author": author,
                         "subject": subject, "body": body}
            by_sha[sha] = c
            commits.append(c)

        # %x00 cannot appear in a path or in git's numstat output, so it safely marks each commit header.
        stats: dict[str, list[str]] = {}
        cur: Optional[list[str]] = None
        for line in stream_git(repo, "log", "--first-parent", "-m", "--numstat", "--no-renames",
                               "--format=%x00%H", branch):
            if line.startswith("\0"):
                cur = stats.setdefault(line[1:].strip(), [])
            elif cur is not None:
                cur.append(line)
        # With --first-parent, -m diffs a merge against its first parent only, so each commit appears once.
        for c in commits:
            info = self.summarize(stats.get(c["sha"], []))
            info.update(c)
            c.clear()
            c.update(info)
        return commits


def load_graph(repo: str, branch: str) -> Graph:
    """Return sha -> (parents, author time, committer time) for every commit reachable from branch."""
    graph: Graph = {}
    for line in stream_git(repo, "log", "--format=%H %P|%at|%ct", branch):
        left, at, ct = line.rstrip("\n").split("|")
        parts = left.split()
        graph[parts[0]] = (tuple(parts[1:]), int(at), int(ct))
    return graph


def load_reverts(repo: str, branch: str) -> Dict[str, str]:
    """Return sha -> sha (or prefix) of the commit it reverts, from 'This reverts commit' lines."""
    out: dict[str, str] = {}
    text = run_git(repo, "log", "--format=%x1e%H%x1f%b", "--grep=This reverts commit", branch, check=False)
    for rec in text.split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, body = rec.split("\x1f", 1)
        m = re.search(r"This reverts commit ([0-9a-f]{7,40})", body)
        if m:
            out[sha.strip()] = m.group(1)
    return out


def build_loc_series(first_parent: List[Commit]) -> Dict[str, Any]:
    """Return cumulative lines of code per UTC day, total and per language.

    Returns:
        {"days": [[day start, total, added, deleted], ...], "langs": language -> per-day lines,
        "kinds": language -> kind}.
    """
    day = None
    days: list[list[int]] = []
    langs_cum: dict[str, int] = collections.defaultdict(int)
    kinds: dict[str, str] = {}
    snapshots: list[Any] = []  # per day: dict of cumulative lang -> lines
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
    peak: collections.Counter[str] = collections.Counter()
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


def claim_branch_commits(first_parent: List[Commit], graph: Graph) -> Dict[str, List[str]]:
    """Map each merge commit on the first-parent line to the commits it brought in."""
    fp_set = {c["sha"] for c in first_parent}
    claimed: set[str] = set()
    result: dict[str, list[str]] = {}
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


def compute_deploys(repo: str, graph: Graph, tag_pattern: str,
                    branch: str) -> Tuple[List[Dict[str, Any]], DeployClaims]:
    """Find tags matching the pattern, and the first such tag that contains each commit.

    Returns (deploys, claimed) where claimed maps commit sha -> (tag time, tag name).
    """
    rx = compile_re(tag_pattern, "--tag-pattern")
    out = run_git(repo, "for-each-ref", "--format=%(refname:short)%09%(creatordate:unix)%09%(objectname)%09%(*objectname)",
                  "refs/tags", check=False)
    tags = []
    for line in out.split("\n"):
        if not line:
            continue
        name, ts, obj, peeled = (line.split("\t") + ["", "", "", ""])[:4]
        sha = peeled or obj
        if not ts or not rx.search(name) or sha not in graph:
            continue
        tags.append((int(ts), name, sha))
    tags.sort()
    claimed: DeployClaims = {}
    deploys: list[dict[str, Any]] = []
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

def infer_prs_from_git(first_parent: List[Commit], graph: Graph, default_branch: str,
                       commits_as_prs: bool = False,
                       claims: Optional[Dict[str, List[str]]] = None) -> Tuple[List[PR], int, int]:
    """Infer PRs from merge commits and squash commits that reference a PR number.

    default_branch is the short branch name (no remote prefix), used to recognise sync merges.
    Returns (prs, direct_commit_count, skipped_sync_merges).
    """
    if claims is None:
        claims = claim_branch_commits(first_parent, graph)
    prs: list[PR] = []
    direct = 0
    sync = 0
    for c in first_parent:
        msg = parse_message(c["subject"], c["body"], default_branch)
        is_merge = len(c["parents"]) >= 2
        if msg["sync"]:
            sync += 1
            continue
        if not is_merge and not msg["has_ref"] and not commits_as_prs:
            direct += 1
            continue
        branch_shas = claims.get(c["sha"], []) if is_merge else []
        branch_commits = [graph[s] for s in branch_shas if len(graph[s][0]) < 2]
        pr: PR = {
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
            "inferred": ["created", "ready", "first_review"],
        }
        if is_merge and branch_commits:
            pr["first_commit"] = min(b[1] for b in branch_commits)
            pr["last_commit"] = max(b[1] for b in branch_commits)
            pr["commits"] = len(branch_commits)
            pr["branch_shas"] = branch_shas
        if pr["title"] is None:
            pr["title"] = humanize_branch(pr["branch"]) or c["subject"]
        prs.append(pr)
    return prs, direct, sync


# ---------------------------------------------------------------------------
# HTTP + API clients
# ---------------------------------------------------------------------------

class ApiError(ToolError):
    """A failed or unexpected GitHub/GitLab API call."""


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect to another host or to plain http, so tokens cannot leak."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: http.client.HTTPMessage, newurl: str) -> Optional[urllib.request.Request]:
        """Follow the redirect only if it stays on the same host and on https (or loopback http)."""
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if new.netloc != old.netloc:
            raise ApiError(f"Refusing cross-host redirect to {new.netloc}")
        loopback_http = old.scheme == "http" and _is_loopback(old.hostname or "")
        if new.scheme != "https" and not (loopback_http and new.scheme == "http"):
            raise ApiError(f"Refusing redirect to insecure URL {new.scheme}://{new.netloc}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirect)
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameHostRedirect)


def _is_loopback(host: str) -> bool:
    """Return True for "localhost" or a loopback IP address."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _retry_after(value: Optional[str], default: int) -> int:
    """Seconds to wait from a Retry-After header (integer seconds or an HTTP date), capped at 60."""
    if not value:
        return default
    value = value.strip()
    try:
        wait = int(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            wait = int(when.timestamp() - time.time())
        except (TypeError, ValueError, IndexError, OverflowError):
            return default
    return min(wait, 60) if wait > 0 else default


def http_json(url: str, headers: Dict[str, str], body: Any = None, retries: int = 4) -> Tuple[Any, Message]:
    """Send a GET (or a JSON POST when body is given) and return (parsed JSON, response headers).

    Retries rate limits, 5xx and transient network errors with backoff; raises ApiError otherwise.
    """
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
            opener = _DIRECT_OPENER if _is_loopback(parsed.hostname or "") else _OPENER
            with opener.open(req, timeout=60) as resp:
                raw = resp.read()
                hdrs_out = resp.headers
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(_retry_after(e.headers.get("Retry-After") if e.headers else None, delay))
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
            # A refused connection or an unknown host will not fix itself: fail fast.
            if attempt < retries and not isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
                time.sleep(delay)
                delay *= 2
                continue
            raise ApiError(f"Could not reach the API: {e.reason}")
        except (OSError, http.client.HTTPException) as e:  # timeouts, resets, truncated responses
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise ApiError(f"Could not reach the API: {e.__class__.__name__}")
        try:
            return json.loads(raw.decode("utf-8")), hdrs_out
        except ValueError:
            raise ApiError("API returned a response that is not JSON (a proxy or login page?)")
    raise ApiError("API request failed")


def parse_iso(s: Optional[str]) -> Optional[int]:
    """Parse an ISO 8601 timestamp into Unix seconds; None or "" gives None."""
    if not s:
        return None
    s = re.sub(r"\.\d+", "", s).replace("Z", "+0000")
    s = re.sub(r"([+-]\d\d):(\d\d)$", r"\1\2", s)
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp())


GITHUB_QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequests(first:50,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}){
      pageInfo{hasNextPage endCursor}
      nodes{
        number title state createdAt updatedAt mergedAt closedAt additions deletions changedFiles
        headRefName baseRefName
        author{login __typename}
        labels(first:20){nodes{name}}
        mergeCommit{oid}
        commits(first:1){totalCount nodes{commit{authoredDate}}}
        reviews(first:50){nodes{submittedAt state author{login __typename}}}
        timelineItems(first:20,itemTypes:[READY_FOR_REVIEW_EVENT]){nodes{... on ReadyForReviewEvent{createdAt}}}
      }
    }
  }
}
"""


def fetch_github(owner: str, name: str, token: str, api_url: str, since: Optional[int], base_branch: str,
                 all_bases: bool, max_prs: Optional[int], progress: Callable[[str], None] = log,
                 info: Optional[Dict[str, int]] = None) -> List[PR]:
    """Fetch pull requests from the GitHub GraphQL API, newest-updated first.

    A PR merged after `since` was updated after it too, so paging stops at the first PR whose last
    activity is older than `since`; collect() then filters by merge (or open) time. `info`, if given,
    receives the number of PRs seen before the base-branch filter under "seen".
    """
    endpoint = api_url.rstrip("/") + "/graphql"
    headers = {"Authorization": f"Bearer {token}"}
    prs: list[PR] = []
    seen = 0
    cursor = None
    while True:
        data, _ = http_json(endpoint, headers, {"query": GITHUB_QUERY,
                                                "variables": {"owner": owner, "name": name, "cursor": cursor}})
        if not isinstance(data, dict):
            raise ApiError("GitHub returned an unexpected response")
        if data.get("errors"):
            msg = "; ".join(str((e or {}).get("message", "?")) for e in data["errors"])[:300]
            raise ApiError(f"GitHub GraphQL error: {msg}")
        repo_node = (data.get("data") or {}).get("repository")
        if not repo_node:
            raise ApiError("GitHub returned no repository: not found, or the token cannot see it.")
        page = repo_node.get("pullRequests")
        if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
            raise ApiError("GitHub returned an unexpected pullRequests shape")
        stop = False
        for n in page["nodes"]:
            if not n:
                continue
            activity = [parse_iso(n.get(k)) for k in ("updatedAt", "createdAt", "mergedAt", "closedAt")]
            if since and max(t for t in activity if t is not None) < since:
                stop = True
                continue
            seen += 1
            if not all_bases and n.get("baseRefName") != base_branch:
                continue
            prs.append(github_pr(n))
            if max_prs and len(prs) >= max_prs:
                stop = True
                break
        progress(f"  fetched {len(prs)} pull requests")
        info_page = page.get("pageInfo") or {}
        if stop or not info_page.get("hasNextPage"):
            break
        cursor = info_page.get("endCursor")
    if info is not None:
        info["seen"] = seen
    return prs


def _is_bot(login: Optional[str], typename: Optional[str] = None) -> bool:
    """Return True for a Bot account type or a login ending in [bot] or -bot."""
    return bool(typename == "Bot" or (login and (login.endswith("[bot]") or login.endswith("-bot"))))


def github_pr(n: Dict[str, Any]) -> PR:
    """Convert one GraphQL pullRequest node into a raw PR dict."""
    author = (n.get("author") or {})
    login = author.get("login")
    created = parse_iso(n["createdAt"])
    ready = created
    for t in (n.get("timelineItems") or {}).get("nodes", []):
        if t and t.get("createdAt"):
            ready = parse_iso(t["createdAt"])
            break
    merged_at = parse_iso(n.get("mergedAt"))
    reviews = [r for r in (n.get("reviews") or {}).get("nodes", [])
               if r and r.get("submittedAt") and r["state"] != "PENDING"
               and (merged_at is None or cast(int, parse_iso(r["submittedAt"])) <= merged_at)  # post-merge reviews do not count
               and (r.get("author") or {}).get("login") != login
               and not _is_bot((r.get("author") or {}).get("login"), (r.get("author") or {}).get("__typename"))]
    first_review = min((cast(int, parse_iso(r["submittedAt"])) for r in reviews), default=None)
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
        "merged": merged_at, "closed": parse_iso(n["closedAt"]),
        "first_commit": first_commit, "last_commit": None, "commits": commits.get("totalCount"),
        "additions": n.get("additions"), "deletions": n.get("deletions"), "files": n.get("changedFiles"),
        "labels": [l["name"] for l in (n.get("labels") or {}).get("nodes", []) if l],
        "reviewed": first_review is not None if state == "merged" else None,
        "source": "api", "inferred": [], "local": None,
    }


def fetch_gitlab(project_path: str, token: str, api_url: str, since: Optional[int], base_branch: str,
                 all_bases: bool, max_prs: Optional[int], want_reviews: bool,
                 progress: Callable[[str], None] = log, info: Optional[Dict[str, int]] = None) -> List[PR]:
    """Fetch merge requests from the GitLab REST API, with per-MR commit and (optionally) review times.

    `info`, if given, receives the number of MRs fetched under "seen".
    """
    project = urllib.parse.quote(project_path, safe="")
    base = f"{api_url.rstrip('/')}/projects/{project}"
    headers = {"PRIVATE-TOKEN": token}
    mrs: list[dict[str, Any]] = []
    page = 1
    while True:
        q: dict[str, Any] = {"state": "all", "scope": "all", "order_by": "updated_at", "sort": "desc", "per_page": 100, "page": page}
        if since:
            # updated_after, not created_after: an MR opened before the cutoff but merged after it still counts.
            q["updated_after"] = dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not all_bases:
            q["target_branch"] = base_branch
        data, hdrs = http_json(f"{base}/merge_requests?{urllib.parse.urlencode(q)}", headers)
        if not isinstance(data, list):
            raise ApiError("GitLab returned an unexpected response for merge_requests")
        mrs.extend(m for m in data if isinstance(m, dict))
        progress(f"  fetched {len(mrs)} merge requests")
        nxt = hdrs.get("X-Next-Page")
        if not data or not nxt or (max_prs and len(mrs) >= max_prs):
            break
        page = int(nxt)
    if max_prs:
        mrs = mrs[:max_prs]
    if info is not None:
        info["seen"] = len(mrs)

    def pages(path: str) -> Iterator[Dict[str, Any]]:
        """Yield every item of a paginated GitLab list endpoint, following X-Next-Page (at most 50 pages)."""
        sep = "&" if "?" in path else "?"
        page = 1
        for _ in range(50):
            data, hdrs = http_json(f"{base}{path}{sep}per_page=100&page={page}", headers)
            if not isinstance(data, list):
                raise ApiError(f"GitLab returned an unexpected response for {path.split('?')[0]}")
            for item in data:
                if isinstance(item, dict):
                    yield item
            nxt = hdrs.get("X-Next-Page")
            if not data or not nxt:
                return
            page = int(nxt)

    def details(mr: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch first commit time, commit count and first review time for one MR.

        API errors are recorded under "commits_error" / "review_error" instead of raised, and the affected
        values stay None (unknown) rather than being read as "no commits" or "no review".
        """
        out: dict[str, Any] = {"first_commit": None, "commits": None, "first_review": None}
        iid = mr["iid"]
        try:
            # GitLab lists MR commits newest first, so every page is needed to find the first commit.
            commits = list(pages(f"/merge_requests/{iid}/commits"))
            times = [parse_iso(c.get("authored_date") or c.get("created_at")) for c in commits]
            times = [t for t in times if t]
            out["first_commit"] = min(times) if times else None
            out["commits"] = len(commits)
        except (ApiError, ValueError, KeyError, TypeError, AttributeError) as e:
            out["commits_error"] = str(e)
        if want_reviews:
            try:
                me = (mr.get("author") or {}).get("username")
                merged_at = parse_iso(mr.get("merged_at"))
                for note in pages(f"/merge_requests/{iid}/notes?sort=asc&order_by=created_at"):
                    a = note.get("author") or {}
                    if note.get("system") or a.get("username") == me or _is_bot(a.get("username"), "Bot" if a.get("bot") else None):
                        continue
                    t = parse_iso(note.get("created_at"))
                    if t is None or (merged_at is not None and t > merged_at):
                        continue  # comments after the merge are not a review
                    out["first_review"] = t
                    break
            except (ApiError, ValueError, KeyError, TypeError, AttributeError) as e:
                out["review_error"] = str(e)
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        extra = list(pool.map(details, mrs))
    if info is not None:
        info["detail_errors"] = sum(1 for ex in extra if "commits_error" in ex or "review_error" in ex)
    result: list[PR] = []
    for mr, ex in zip(mrs, extra):
        review_known = want_reviews and "review_error" not in ex
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
            "reviewed": (ex["first_review"] is not None) if (state == "merged" and review_known) else None,
            "source": "api", "inferred": ["ready"] + ([] if review_known else ["first_review"]),
            "squash_sha": mr.get("squash_commit_sha"), "local": None,
        })
    return result


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def risk_for(pr: Dict[str, Any]) -> Tuple[int, str, List[RiskFactor]]:
    """Score a PR 0..100 using the additive heuristic in RISK. Returns (score, level, factors)."""
    factors: list[RiskFactor] = []

    def add(name: str, pts: int, detail: str) -> None:
        """Record a factor if it contributes any points."""
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


def local_summary(pr: PR, fp_by_sha: Dict[str, Commit]) -> Optional[Commit]:
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


# "Revert ...", "Reverts #12", "Reverting ..."; not "Revertible settings".
REVERT_TITLE = re.compile(r"^\s*revert(s|ed|ing)?\b", re.I)
CC_PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]|[A-Za-z]+(?:\([^)]*\))?!?\s*:)\s*")


def is_failure(rx: re.Pattern[str], title: Optional[str], branch: Optional[str], labels: Optional[List[str]],
               title_start_only: bool) -> bool:
    """Return True if the PR looks like a hotfix or rollback.

    Branch and labels match anywhere. With title_start_only (the default pattern), the title only matches
    at its start (after an optional Conventional Commit prefix), so "feat: add rollback support" is not a
    hotfix.
    """
    if rx.search(" ".join(filter(None, [branch, " ".join(labels or [])]))):
        return True
    title = title or ""
    if not title_start_only:
        return bool(rx.search(title))
    return any(rx.match(t) for t in (title, title.lstrip("[( "), CC_PREFIX.sub("", title, count=1)))


def build_records(prs: List[PR], ctx: Dict[str, Any]) -> List[Record]:
    """Turn raw PR dicts into the final records embedded in the dashboard.

    Args:
        prs: raw PRs; API PRs may be filled in (size, branch_shas) from the local commit.
        ctx: failure_re, failure_title_start, bot_re, fp_by_sha, claimed, claims, redact and reverts,
            as built by collect().
    """
    failure_re = ctx["failure_re"]
    fp_by_sha = ctx["fp_by_sha"]
    claimed = ctx["claimed"]
    claims = ctx.get("claims") or {}
    records: list[Record] = []
    sha_to_pr: dict[str, Record] = {}
    for pr in prs:
        c = local_summary(pr, fp_by_sha)
        if c is not None and pr["source"] == "api":
            if pr["additions"] is None:  # GitLab: size comes from the local commit
                pr["additions"], pr["deletions"], pr["files"] = c["add"], c["del"], c["files"]
            if len(c["parents"]) >= 2 and not pr.get("branch_shas"):
                # merge-commit workflow: the branch commits (e.g. a revert) resolve time to restore
                pr["branch_shas"] = claims.get(c["sha"], [])
        t, scope, breaking, tsrc = classify_type(pr["title"] or "", pr.get("branch"), pr.get("labels"))
        kind = None
        if t == "revert" or REVERT_TITLE.match(pr["title"] or ""):
            kind = "revert"
        elif is_failure(failure_re, pr["title"], pr.get("branch"), pr.get("labels"), ctx.get("failure_title_start", False)):
            kind = "hotfix"
        add_, del_ = pr["additions"], pr["deletions"]
        lines = (add_ + del_) if add_ is not None and del_ is not None else None
        rec: Record = {
            "id": pr["number"] if pr["number"] is not None else pr["sha"][:8],
            "number": pr["number"],
            "title": pr["title"] if not ctx["redact"] else (f"#{pr['number']}" if pr["number"] is not None else pr["sha"][:8]),
            "type": t, "scope": None if ctx["redact"] else scope, "breaking": breaking, "type_source": tsrc,
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
        def span(a: Optional[int], b: Optional[int]) -> Optional[int]:
            """Return b - a, or None if either is missing or b is before a."""
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
        if rec["lead_to_deploy"] is not None and pr["source"] != "api":
            rec["inferred"].append("lead_to_deploy")  # built from the inferred first-commit time
        rec["restore"] = None
        rec["_sha"] = pr.get("sha")
        rec["_c"] = c
        records.append(rec)
        if merged is not None:
            for s in {pr.get("sha"), pr.get("squash_sha")} - {None}:
                sha_to_pr[cast(str, s)] = rec
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

def embed_json(obj: Any) -> str:
    """Serialise obj as JSON that is safe to embed inside an HTML <script> element."""
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
               .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_html(data: Dict[str, Any]) -> str:
    """Return template.html with the dashboard data embedded at its data marker."""
    if not TEMPLATE_PATH.exists():
        raise ToolError(f"Missing {TEMPLATE_PATH.name} next to repo_stats.py")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    marker = "/*__DATA__*/null"
    if marker not in template:
        raise ToolError("template.html is missing the data marker")
    return template.replace(marker, embed_json(data), 1)


def write_csv(path: str, records: Iterable[Record]) -> None:
    """Write one row per PR record to a CSV file, guarding against spreadsheet formula injection."""
    cols = ["id", "title", "type", "scope", "breaking", "state", "bot", "created", "first_commit", "first_review",
            "merged", "additions", "deletions", "files", "commits", "risk", "risk_level", "kind",
            "coding", "pickup", "review", "open_to_merge", "lead", "deployed", "deploy_wait", "lead_to_deploy",
            "restore", "source"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in records:
            row: list[Any] = []
            for c in cols:
                v = r.get(c)
                if isinstance(v, str) and v and (v[0] in "=+-@\t\r" or v.lstrip()[:1] in ("=", "+", "-", "@")):
                    v = "'" + v  # neutralise spreadsheet formula injection, also after leading spaces
                row.append("" if v is None else v)
            w.writerow(row)


def resolve_token(env_names: Iterable[str], environ: Mapping[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (token, variable name) from the first non-empty variable in `environ`, or (None, None)."""
    for name in env_names:
        v = environ.get(name)
        if v:
            return v.strip(), name
    return None, None


def parse_date(s: Optional[str]) -> Optional[int]:
    """Parse a YYYY-MM-DD date as UTC midnight in Unix seconds; empty gives None."""
    if not s:
        return None
    try:
        return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
    except ValueError:
        raise ToolError(f"Invalid date '{s}'. Use YYYY-MM-DD.")


def collect(args: argparse.Namespace, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Gather all dashboard data for the parsed command line.

    Args:
        args: Parsed command line.
        environ: Environment to read API tokens from; defaults to os.environ.

    Returns:
        {"meta": ..., "prs": [record, ...], "loc": build_loc_series(...), "deploys": [...]}.
    """
    repo = os.path.abspath(args.repo)
    if run_git(repo, "rev-parse", "--is-inside-work-tree", check=False).strip() != "true" \
            and run_git(repo, "rev-parse", "--is-bare-repository", check=False).strip() != "true":
        raise ToolError(f"{repo} is not a git repository")
    branch = args.branch or detect_branch(repo, args.remote)
    if not run_git(repo, "rev-parse", "--verify", "-q", branch + "^{commit}", check=False).strip():
        raise ToolError(f"Branch or ref '{branch}' not found. Pass --branch.")
    short_branch = strip_remote_prefix(repo, branch)
    warnings: list[str] = []
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

    deploys: list[dict[str, Any]] = []
    claimed: DeployClaims = {}
    if args.deploy_source == "tags":
        deploys, claimed = compute_deploys(repo, graph, args.tag_pattern, branch)
        log(f"  {len(deploys)} release tags")
    else:
        for c in first_parent:
            claimed[c["sha"]] = (c["ct"], "merge")
    claims = claim_branch_commits(first_parent, graph)

    # Decide where PR data comes from.
    remote = run_git(repo, "remote", "get-url", args.remote, check=False).strip()
    parsed = parse_remote(remote) if remote else None
    host_provider = detect_provider(parsed[0]) if parsed else None
    provider = args.provider or host_provider
    untrusted = None
    if parsed and provider and provider != host_provider and not args.api_url:
        # Never derive an API host from an arbitrary remote: a token would go to whoever controls it.
        untrusted = (f"Remote host {parsed[0]} is not github.com or gitlab.com; pass --provider and --api-url "
                     "to use its API.")
    elif parsed and not provider and parsed[0] not in TRUSTED_HOSTS:
        untrusted = (f"Remote host {parsed[0]} is not github.com or gitlab.com; pass --provider and --api-url "
                     "to use its API.")
    token_envs = [args.token_env] if args.token_env else (
        ["GITHUB_TOKEN", "GH_TOKEN"] if provider == "github" else
        ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"] if provider == "gitlab" else [])
    token, token_name = resolve_token(token_envs, os.environ if environ is None else environ)

    source = args.source
    api_prs: Optional[List[PR]] = None
    if source in ("auto", "api"):
        if not parsed or not provider or untrusted:
            msg = "No GitHub or GitLab remote was detected (use --remote, or --provider with --api-url)."
            if untrusted:
                msg += " " + untrusted
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
            info: dict[str, int] = {}
            try:
                log(f"Fetching pull requests from {provider} API (token from ${token_name}) ...")
                if provider == "github":
                    if "/" not in path:
                        raise ApiError(f"Cannot read owner/name from the remote path '{path}'")
                    owner, name = path.split("/", 1)
                    api_url = args.api_url or "https://api.github.com"
                    api_prs = fetch_github(owner, name, token, api_url, since, short_branch, args.all_bases, args.max_prs,
                                           info=info)
                else:
                    api_url = args.api_url or "https://gitlab.com/api/v4"
                    api_prs = fetch_gitlab(path, token, api_url, since, short_branch, args.all_bases, args.max_prs,
                                           not args.no_reviews, info=info)
            except (ApiError, ValueError, KeyError, TypeError, AttributeError, IndexError, OSError) as e:
                detail = str(e) if isinstance(e, ApiError) else f"unexpected response or I/O error: {e.__class__.__name__}: {e}"
                if source == "api":
                    raise ApiError(f"API fetch failed: {detail}")
                warnings.append(f"API fetch failed ({detail}). Using PRs inferred from git history instead.")
                api_prs = None
            if info.get("detail_errors"):
                warnings.append(f"{info['detail_errors']} merge request(s) had commit or review lookups fail. "
                                "Their coding time, lead time and review status are left empty, not guessed.")
            if api_prs is not None and not api_prs:
                seen = info.get("seen", 0)
                if seen and not args.all_bases:
                    warnings.append(f"The API returned {seen} pull request(s) but none target '{short_branch}'. "
                                    "Pass --branch with the base branch name, or --all-bases.")
                else:
                    warnings.append("The API returned no pull requests for this repository"
                                    + (" in the selected date range." if since else "."))

    direct = sync = 0
    if api_prs is not None:
        prs, mode = api_prs, "api"
    else:
        prs, direct, sync = infer_prs_from_git(first_parent, graph, short_branch, args.commits_as_prs, claims)
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
    if until:  # --until is inclusive of that whole UTC day
        prs = [p for p in prs if (p["merged"] or p["created"] or 0) < until + 86400]
    if args.max_prs and mode == "git":
        prs = prs[-args.max_prs:]

    records = build_records(prs, {
        "failure_re": failure_re, "failure_title_start": args.failure_pattern is None,
        "bot_re": bot_re, "fp_by_sha": fp_by_sha, "claimed": claimed, "claims": claims,
        "redact": args.no_titles, "reverts": load_reverts(repo, branch),
    })
    if args.exclude_bots:
        records = [r for r in records if not r["bot"]]
    records.sort(key=lambda r: r["merged"] or r["created"] or 0)

    if since:
        deploys = [d for d in deploys if d["t"] >= since]
    if until:
        deploys = [d for d in deploys if d["t"] < until + 86400]
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
        "failure_pattern": args.failure_pattern or DEFAULT_FAILURE, "failure_default": args.failure_pattern is None,
        "tag_default": args.tag_pattern == DEFAULT_TAG_PATTERN, "excludes": excludes,
        "reviews": mode == "api" and not (provider == "gitlab" and args.no_reviews),
        "commits_as_prs": bool(args.commits_as_prs),
        "heuristics": {"risk": RISK, "size_buckets": SIZE_BUCKETS, "sensitive": args.sensitive_pattern or DEFAULT_SENSITIVE,
                       "test_path": TEST_PATH.pattern, "doc_path": DOC_PATH.pattern},
    }
    return {"meta": meta, "prs": records, "loc": loc, "deploys": deploys}


def positive_int(value: str) -> int:
    """argparse type: an integer of at least 1."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {n}")
    return n


def port_number(value: str) -> int:
    """argparse type: a TCP port from 0 (any free port) to 65535."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}")
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0-65535, got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
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
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only include PRs merged (or opened, if unmerged) on or after this UTC date")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only include PRs merged (or opened) on or before this UTC date (inclusive)")
    g = p.add_argument_group("PR data source")
    g.add_argument("--source", choices=["auto", "git", "api"], default="auto",
                   help="auto uses the API when a token is available, otherwise infers PRs from git (default: auto)")
    g.add_argument("--provider", choices=["github", "gitlab"], help="hosting provider (default: detected from the remote)")
    g.add_argument("--api-url", help="API base URL for self-hosted servers, e.g. https://host/api/v4 (GitLab) or https://host/api (GitHub Enterprise)")
    g.add_argument("--token-env", metavar="NAME", help="environment variable that holds the API token")
    g.add_argument("--no-reviews", action="store_true", help="GitLab: skip per-MR review lookups (faster, fewer API calls)")
    g.add_argument("--all-bases", action="store_true", help="include PRs that target branches other than the analysed one")
    g.add_argument("--max-prs", type=positive_int, metavar="N", help="stop after N PRs (newest first)")
    g.add_argument("--commits-as-prs", action="store_true",
                   help="git mode: treat every direct commit on the branch as a PR (for repos without PR references)")
    g = p.add_argument_group("heuristics")
    g.add_argument("--exclude", action="append", metavar="REGEX", help="extra path regex to ignore for lines of code and size (repeatable)")
    g.add_argument("--no-default-excludes", action="store_true", help="count lockfiles, vendored and generated files too")
    g.add_argument("--deploy-source", choices=["tags", "merges"], default="tags",
                   help="what counts as a deployment: release tags, or every merge to the branch (default: tags)")
    g.add_argument("--tag-pattern", default=DEFAULT_TAG_PATTERN, metavar="REGEX", help=f"tags that count as releases (default: {DEFAULT_TAG_PATTERN})")
    g.add_argument("--failure-pattern", metavar="REGEX", help=f"regex marking a hotfix, searched in title, branch and labels (default: {DEFAULT_FAILURE}, which "
                        "matches branch/labels anywhere but the title only at its start)")
    g.add_argument("--sensitive-pattern", metavar="REGEX", help="path regex that raises risk (default: migrations, auth, infra, CI)")
    g.add_argument("--bot-pattern", default=r"\[bot\]$|^dependabot|^renovate|-bot$", metavar="REGEX", help="author regex that marks a bot")
    g.add_argument("--exclude-bots", action="store_true", help="drop bot-authored PRs entirely")
    g = p.add_argument_group("privacy and viewing")
    g.add_argument("--no-titles", action="store_true", help="replace PR titles with their number in the output")
    g.add_argument("--serve", nargs="?", const=8000, type=port_number, metavar="PORT", help="serve the result on localhost and open a browser")
    g.add_argument("--no-open", action="store_true", help="with --serve, do not open a browser")
    p.add_argument("--version", action="version", version=f"repo-stats {__version__}")
    return p


# Same policy as the template's meta tag: inline script and style only, no network access at all.
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
       "base-uri 'none'; form-action 'none'")


def serve(path: Path, port: int, open_browser: bool) -> None:
    """Serve only the dashboard file: `/` and `/<name>` return it, everything else is 404 (no listing).

    Requests whose Host header is not 127.0.0.1 or localhost on the bound port get 421 (DNS rebinding).
    """
    target = path.resolve()
    allowed = {"/", "/" + urllib.parse.quote(path.name), "/" + path.name}
    allowed_hosts: Set[str] = set()  # filled once the server is bound and its real port is known

    class Handler(http.server.BaseHTTPRequestHandler):
        """Request handler that returns the dashboard file for allowed paths only."""

        def _send(self, head_only: bool) -> None:
            """Send the dashboard (headers only if head_only), 421 for a foreign Host, or 404."""
            host = self.headers.get("Host")
            if not host or host.lower() not in allowed_hosts:
                self.send_error(421, "Misdirected Request")
                return
            if urllib.parse.urlsplit(self.path).path not in allowed:
                self.send_error(404, "Not Found")
                return
            try:
                body = target.read_bytes()
            except OSError:
                self.send_error(404, "Not Found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def do_GET(self) -> None:
            """Handle GET."""
            self._send(False)

        def do_HEAD(self) -> None:
            """Handle HEAD."""
            self._send(True)

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default per-request logging."""

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    bound = httpd.server_address[1]
    allowed_hosts.update({f"127.0.0.1:{bound}", f"localhost:{bound}"})
    url = f"http://127.0.0.1:{bound}/{urllib.parse.quote(path.name)}"
    log(f"Serving {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the command line and return the process exit code."""
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
    except OSError as e:  # unwritable output, missing git binary, ...
        where = f" ({e.filename})" if getattr(e, "filename", None) else ""
        log(f"error: {e.strerror or e}{where}")
        return 1
    merged = sum(1 for r in data["prs"] if r["state"] == "merged")
    log(f"Wrote {out} ({len(data['prs'])} PRs, {merged} merged, source: {data['meta']['mode']})")
    for w in data["meta"]["warnings"]:
        log(f"warning: {w}")
    if args.serve is not None:
        try:
            serve(out, args.serve, not args.no_open)
        except OSError as e:
            log(f"error: could not serve on 127.0.0.1:{args.serve}: {e.strerror or e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
