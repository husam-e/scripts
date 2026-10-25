"""Builds small synthetic git repositories for tests. No real project data is used."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

DAY = 86400
T0 = 1_640_995_200  # 2022-01-01T00:00:00Z


class Repo:
    """A throwaway git repository with a controllable commit clock."""

    def __init__(self, path: str | Path | None = None) -> None:
        """Initialise an empty repo on branch main with a fixed test identity.

        Args:
            path: Directory to use; a fresh temporary directory when omitted.
        """
        self.path = Path(path or tempfile.mkdtemp(prefix="repostats-fixture-"))
        self.clock = T0
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Dev One")
        self.git("config", "user.email", "dev@example.com")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str, date: int | None = None, check: bool = True) -> str:
        """Run a git command in the repo with author and committer dates pinned.

        Args:
            args: Arguments passed to git.
            date: Unix time for both dates; defaults to the repo clock.
            check: Raise RuntimeError when git exits non-zero.

        Returns:
            git's stdout, stripped.
        """
        env = dict(os.environ)
        d = date if date is not None else self.clock
        iso = f"{d} +0000"
        env.update(GIT_AUTHOR_DATE=iso, GIT_COMMITTER_DATE=iso)
        p = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True, env=env)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {args} failed: {p.stderr}")
        return p.stdout.strip()

    def advance(self, seconds: int) -> None:
        """Move the repo clock forward by `seconds`."""
        self.clock += seconds

    def write(self, rel: str, text: str) -> None:
        """Write `text` to the file at relative path `rel`, creating parent directories."""
        f = self.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def commit(self, msg: str, files: Mapping[str, str] | None = None, advance: int = 3600) -> str:
        """Write `files`, commit all changes with message `msg`, then advance the clock.

        Args:
            msg: Commit message.
            files: Map of relative path to new file content.
            advance: Seconds to move the clock after committing.

        Returns:
            The new commit's SHA.
        """
        for rel, text in (files or {}).items():
            self.write(rel, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        self.advance(advance)
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, annotated: bool = False) -> None:
        """Tag HEAD as `name`, as an annotated tag when `annotated` is true."""
        if annotated:
            self.git("tag", "-a", name, "-m", name)
        else:
            self.git("tag", name)

    def merge_pr(self, number: int, branch: str, title: str, commits: Iterable[tuple[str, Mapping[str, str]]],
                 merge_after: int = 7200, user: str = "dev") -> str:
        """Simulate a GitHub 'merge commit' PR: branch commits, then a --no-ff merge into main.

        Args:
            number: PR number used in the merge subject.
            branch: Feature branch name.
            title: PR title, written as the merge body.
            commits: (message, files) pairs committed on the branch.
            merge_after: Seconds between the last branch commit and the merge.
            user: Branch owner shown in the merge subject.

        Returns:
            The merge commit's SHA.
        """
        self.git("checkout", "-q", "-b", branch)
        for msg, files in commits:
            self.commit(msg, files)
        self.advance(merge_after)
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-q", branch, "-m", f"Merge pull request #{number} from {user}/{branch}", "-m", title)
        self.git("branch", "-q", "-D", branch)
        self.advance(3600)
        return self.git("rev-parse", "HEAD")

    def squash_pr(self, number: int, title: str, files: Mapping[str, str], body: str = "") -> str:
        """Simulate a GitHub squash merge: one commit on main titled 'title (#number)'.

        Returns:
            The new commit's SHA.
        """
        msg = f"{title} (#{number})"
        self.write("x", "")
        os.remove(self.path / "x")
        return self.commit(msg + (f"\n\n{body}" if body else ""), files)


def lines(n: int, prefix: str = "line") -> str:
    """Return `n` numbered text lines, each ending in a newline."""
    return "".join(f"{prefix} {i}\n" for i in range(n))


def merge_workflow_repo() -> Repo:
    """Build a small merge-commit history: PRs #1-#5 (one revert, one hotfix), two tags and direct commits."""
    r = Repo()
    r.commit("chore: initial commit", {"README.md": lines(5), "src/app.py": lines(40)})
    r.advance(DAY)
    r.merge_pr(1, "feat/login", "feat(auth): add login", [("wip", {"src/auth/login.py": lines(80)}), ("more", {"tests/test_login.py": lines(30)})])
    r.advance(DAY)
    r.merge_pr(2, "fix/typo", "fix: correct typo", [("typo", {"README.md": lines(6)})])
    r.tag("v1.0.0", annotated=True)
    r.advance(2 * DAY)
    sha = r.merge_pr(3, "feat/big", "feat!: rework engine", [("engine", {"src/engine.py": lines(700), "src/extra.go": lines(50)})])
    r.advance(DAY)
    r.git("checkout", "-q", "-b", "revert-3")
    r.git("revert", "--no-edit", "-m", "1", sha)
    r.git("checkout", "-q", "main")
    r.git("merge", "--no-ff", "-q", "revert-3", "-m", "Merge pull request #4 from dev/revert-3", "-m", 'Revert "feat!: rework engine"')
    r.advance(DAY)
    r.merge_pr(5, "hotfix/crash", "fix: hotfix crash on start", [("crash", {"src/app.py": lines(41)})])
    r.tag("v1.1.0")
    r.advance(DAY)
    r.commit("docs: tweak readme directly", {"README.md": lines(7)})
    return r


def squash_workflow_repo() -> Repo:
    """Build a squash-merge history: PRs #2-#7, a revert of #2 as #8, and one tag."""
    r = Repo()
    r.commit("chore: initial commit", {"app.js": lines(20)})
    r.advance(DAY)
    for i in range(2, 8):
        kind = ["feat", "fix", "docs", "refactor", "test", "chore"][i % 6]
        r.squash_pr(i, f"{kind}: change number {i}", {f"f{i}.js": lines(10 * i)})
        r.advance(DAY * (i % 3 + 1))
    r.squash_pr(8, "Revert \"feat: change number 2\"", {"f2.js": lines(1)},
                body="This reverts commit " + r.git("log", "--format=%H", "--grep", "change number 2").splitlines()[0] + ".")
    r.tag("v0.1.0")
    return r


def synthetic_repo(n_prs: int = 150, seed: int = 7, days: int = 540, style: str = "merge") -> Repo:
    """Build a larger deterministic history with varied sizes, types, reverts and releases.

    Args:
        n_prs: Number of PRs.
        seed: Random seed.
        days: Rough time span of the history.
        style: "merge" for --no-ff merge commits, anything else for squash commits.
    """
    import random
    rnd = random.Random(seed)
    r = Repo()
    r.commit("chore: initial commit", {"README.md": lines(10), "src/core.py": lines(200)})
    types = ["feat"] * 5 + ["fix"] * 4 + ["refactor", "docs", "test", "chore", "perf", "ci"]
    exts = [".py", ".py", ".js", ".go", ".md", ".yaml"]
    step = days * DAY // n_prs
    last_merged = []
    for i in range(1, n_prs + 1):
        t = rnd.choice(types)
        if rnd.random() < 0.05 and last_merged:
            target = last_merged.pop()
            title = f'Revert "{target[1]}"'
            files = {f"src/rev{i}.py": lines(rnd.randint(1, 30))}
            br = f"revert-{i}"
            r.git("checkout", "-q", "-b", br)
            r.commit(title + "\n\nThis reverts commit " + target[0] + ".", files)
            r.git("checkout", "-q", "main")
            r.git("merge", "--no-ff", "-q", br, "-m", f"Merge pull request #{i} from dev/{br}", "-m", title)
            r.git("branch", "-q", "-D", br)
        else:
            size = int(min(3000, rnd.lognormvariate(3.8, 1.2))) + 1
            nfiles = min(12, 1 + int(rnd.expovariate(0.6)))
            title = f"{t}: change {i}"
            if rnd.random() < 0.15:
                title = f"{t}!: breaking change {i}"
            files = {}
            for k in range(nfiles):
                folder = rnd.choice(["src", "src", "lib", "tests", "docs", "src/auth", "migrations"])
                files[f"{folder}/m{i}_{k}{rnd.choice(exts)}"] = lines(max(1, size // nfiles))
            if style == "merge":
                br = f"{t}/work-{i}"
                r.git("checkout", "-q", "-b", br)
                for c in range(rnd.randint(1, 4)):
                    sub = {k: v + f"rev {c}\n" for k, v in list(files.items())[c::4]} or {f"src/w{i}_{c}.py": lines(3)}
                    r.commit(f"wip {i}.{c}", sub, advance=rnd.randint(600, 40000))
                r.advance(rnd.randint(3600, 3 * DAY))
                r.git("checkout", "-q", "main")
                r.git("merge", "--no-ff", "-q", br, "-m", f"Merge pull request #{i} from dev/{br}", "-m", title)
                r.git("branch", "-q", "-D", br)
                sha = r.git("rev-parse", "HEAD")
            else:
                sha = r.commit(f"{title} (#{i})", files, advance=0)
            last_merged.append((sha, title))
        r.advance(step + rnd.randint(-step // 3, step // 3))
        if i % 15 == 0:
            r.tag(f"v1.{i // 15}.0", annotated=bool(i % 30))
    return r
