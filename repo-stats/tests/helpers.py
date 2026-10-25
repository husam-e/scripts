"""Shared helpers for the repo-stats tests. Stdlib only."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402
import repo_stats as rs  # noqa: E402

SCRIPT = str(ROOT / "repo_stats.py")
TOKEN_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN")
CACHE = Path(tempfile.gettempdir()) / "repostats-test-cache"


def clean_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of os.environ without API tokens, with loopback exempt from proxies, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if k not in TOKEN_VARS}
    # loopback must never go through a proxy
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost," + env.get("NO_PROXY", "")
    env.update(extra or {})
    return env


@contextlib.contextmanager
def patched_env(extra: Mapping[str, str] | None = None) -> Iterator[None]:
    """Remove any real token from os.environ for the duration, optionally set fake ones."""
    saved = dict(os.environ)
    try:
        for k in TOKEN_VARS:
            os.environ.pop(k, None)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost," + saved.get("NO_PROXY", "")
        os.environ.update(extra or {})
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def run_cli(args: Iterable[object], env: Mapping[str, str] | None = None, timeout: float = 120,
            cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a subprocess with real tokens stripped. Returns CompletedProcess."""
    return subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True,
                          env=clean_env(env), timeout=timeout, cwd=cwd)


def collect(repo: fixtures.Repo | str | Path, *extra: str, env: Mapping[str, str] | None = None) -> tuple[dict[str, Any], str]:
    """In-process collect() with stderr swallowed. Returns (data, stderr_text)."""
    path = getattr(repo, "path", repo)
    args = rs.build_parser().parse_args([str(path), *extra])
    buf = io.StringIO()
    with patched_env(env), contextlib.redirect_stderr(buf):
        data = rs.collect(args)
    return data, buf.getvalue()


def by_number(data: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Index a collect() result's PRs by PR number."""
    return {p["number"]: p for p in data["prs"]}


def add_remote(repo: fixtures.Repo, url: str, name: str = "origin") -> fixtures.Repo:
    """Add a git remote called `name` to `repo` and return the repo."""
    repo.git("remote", "add", name, url)
    return repo


def first_parent_shas(repo: fixtures.Repo) -> list[tuple[str, ...]]:
    """Return (sha, subject) pairs for main's first-parent history, oldest first."""
    out = repo.git("log", "--first-parent", "--reverse", "--format=%H\t%s", "main")
    return [tuple(l.split("\t", 1)) for l in out.splitlines()]


def real_line_count(path: str | Path, exclude_regexes: Iterable[str] = ()) -> int:
    """Sum of line counts of text files tracked at HEAD (binary files skipped)."""
    import re
    rxs = [re.compile(x) for x in exclude_regexes]
    out = subprocess.run(["git", "-C", str(path), "-c", "core.quotepath=off", "ls-files", "-z"],
                         capture_output=True, check=True).stdout.decode("utf-8", "replace")
    total = 0
    for rel in filter(None, out.split("\0")):
        if any(r.search(rel) for r in rxs):
            continue
        raw = (Path(path) / rel).read_bytes()
        if b"\0" in raw:
            continue
        total += len(raw.splitlines())
    return total


def cached_synthetic(n: int, style: str = "merge") -> Path:
    """Return the path of a synthetic_repo(n, style=style) copy cached on disk across test runs.

    synthetic_repo() is deterministic but slow to build, so it is built once and reused.
    """
    key = CACHE / f"synthetic-{n}-{style}-v1"
    if not (key / ".ready").exists():
        shutil.rmtree(key, ignore_errors=True)
        r = fixtures.synthetic_repo(n, style=style)
        CACHE.mkdir(parents=True, exist_ok=True)
        shutil.copytree(r.path, key / "repo", symlinks=True)
        (key / ".ready").write_text("ok")
        shutil.rmtree(r.path, ignore_errors=True)
    return key / "repo"


def fast_import_repo(n_prs: int, start: int = 1_640_995_200, step: int = 3 * 3600) -> Path:
    """Very fast merge-style history (git fast-import) for performance tests. Returns a path."""
    path = Path(tempfile.mkdtemp(prefix="repostats-fastimport-"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    out = []
    mark = [0]

    def commit(ref: str, msg: str, t: int, files: Mapping[str, str], frm: str | None = None,
               merge: str | None = None) -> str:
        """Append one fast-import commit to the stream and return its mark.

        Args:
            ref: Branch ref the commit goes on.
            msg: Commit message.
            t: Committer time in Unix seconds.
            files: Map of path to full file content.
            frm: Mark of the first parent.
            merge: Mark of the second parent, for merge commits.
        """
        mark[0] += 1
        out.append(f"commit {ref}\nmark :{mark[0]}\ncommitter Dev <dev@example.com> {t} +0000\n")
        data = msg.encode()
        out.append(f"data {len(data)}\n{msg}\n")
        if frm:
            out.append(f"from {frm}\n")
        if merge:
            out.append(f"merge {merge}\n")
        for p, body in files.items():
            b = body.encode()
            out.append(f"M 100644 inline {p}\ndata {len(b)}\n{body}\n")
        out.append("\n")
        return f":{mark[0]}"

    t = start
    tip = commit("refs/heads/main", "chore: initial commit", t, {"README.md": "x\n" * 5})
    for i in range(1, n_prs + 1):
        t += step
        br = f"refs/heads/feat/w{i}"
        b1 = commit(br, f"wip {i}.0", t, {f"src/m{i}.py": "line\n" * (3 + i % 40)}, frm=tip)
        t += 1800
        b2 = commit(br, f"wip {i}.1", t, {f"tests/t{i}.py": "t\n" * 4}, frm=b1)
        t += 1800
        tip = commit("refs/heads/main", f"Merge pull request #{i} from dev/feat/w{i}\n\nfeat: change {i}", t,
                     {f"src/m{i}.py": "line\n" * (3 + i % 40), f"tests/t{i}.py": "t\n" * 4}, frm=tip, merge=b2)
        if i % 20 == 0:
            out.append(f"tag v1.{i // 20}.0\nfrom {tip}\ntagger Dev <dev@example.com> {t} +0000\ndata 4\nrel\n\n")
    subprocess.run(["git", "-C", str(path), "fast-import", "--quiet", "--force"], input="".join(out).encode(),
                   check=True)
    return path
