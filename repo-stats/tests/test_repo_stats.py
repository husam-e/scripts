"""Python-side tests for repo_stats.py (plan sections A, B, C and the non-browser parts of E).

Run:  python3 -m unittest discover -s tests -v
Stdlib only. Deterministic. No real network: API tests run fake gh/glab CLIs (tests/fake_cli) that
forward to 127.0.0.1 mock servers, with fake tokens.
"""
from __future__ import annotations

import ast
import contextlib
import csv
import http.client
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence, TypeVar
from unittest import mock

import helpers
from helpers import (ROOT, SCRIPT, add_remote, by_number, collect, first_parent_shas, real_line_count, run_cli,
                     rs)
import fixtures
from fixtures import DAY, T0, Repo, lines
import mock_servers as ms

T = TypeVar("T")

FAKE = "fake-token-AbC123xyZ-not-a-real-secret"
_cache: dict = {}


def cached(key: str, builder: Callable[[], T]) -> T:
    """Build a value with `builder` once per test run and memoise it under `key`."""
    if key not in _cache:
        _cache[key] = builder()
    return _cache[key]


def merge_repo() -> Repo:
    """Return the shared merge_workflow_repo() fixture, built on first use."""
    return cached("merge", fixtures.merge_workflow_repo)


def squash_repo() -> Repo:
    """Return the shared squash_workflow_repo() fixture, built on first use."""
    return cached("squash", fixtures.squash_workflow_repo)


def merge_data() -> dict[str, Any]:
    """Return collect() output for the shared merge-workflow repo, computed once."""
    return cached("merge-data", lambda: collect(merge_repo())[0])


def squash_data() -> dict[str, Any]:
    """Return collect() output for the shared squash-workflow repo, computed once."""
    return cached("squash-data", lambda: collect(squash_repo())[0])


def git_out(path: str | Path, *args: str) -> str:
    """Run git in `path` and return its stripped stdout; raise on failure."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def extract_data(html: str) -> dict[str, Any]:
    """Parse the JSON embedded as `const DATA = ...;` in a generated dashboard."""
    start = html.index("const DATA = ") + len("const DATA = ")
    end = html.index(";\n", start)
    return json.loads(html[start:end])


def tmpdir() -> Path:
    """Create a fresh temporary output directory."""
    d = tempfile.mkdtemp(prefix="repostats-out-")
    return Path(d)


# =====================================================================================================
# A. Git-only data correctness
# =====================================================================================================

class A1_PRInferenceMerge(unittest.TestCase):
    """PR inference from a merge-commit history."""

    def test_five_prs_numbers_titles_types(self) -> None:
        """Merge commits yield PRs #1-#5 with the right titles and types."""
        d = merge_data()
        prs = by_number(d)
        self.assertEqual(sorted(prs), [1, 2, 3, 4, 5])
        self.assertEqual(prs[1]["title"], "feat(auth): add login")
        self.assertEqual(prs[2]["title"], "fix: correct typo")
        self.assertEqual(prs[3]["title"], "feat!: rework engine")
        self.assertEqual(prs[4]["title"], 'Revert "feat!: rework engine"')
        self.assertEqual(prs[5]["title"], "fix: hotfix crash on start")
        self.assertEqual([prs[i]["type"] for i in range(1, 6)], ["feat", "fix", "feat", "revert", "fix"])

    def test_breaking_and_kind(self) -> None:
        """Breaking changes, reverts and hotfixes are flagged."""
        prs = by_number(merge_data())
        self.assertTrue(prs[3]["breaking"])
        self.assertEqual([prs[i]["breaking"] for i in (1, 2, 4, 5)], [False] * 4)
        self.assertEqual(prs[4]["kind"], "revert")
        self.assertEqual(prs[5]["kind"], "hotfix")
        self.assertIsNone(prs[1]["kind"])

    def test_direct_commits_counted(self) -> None:
        """Commits made directly on main are counted as direct commits."""
        # The fixture has the root commit AND "docs: tweak readme directly" on main, i.e. 2 direct commits.
        # (The plan says 1; the root commit is also a direct commit, 2 is the correct count.)
        self.assertEqual(merge_data()["meta"]["direct_commits"], 2)

    def test_mode_and_source(self) -> None:
        """Git mode marks every PR as a merged merge-commit PR."""
        d = merge_data()
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(all(p["source"] == "merge-commit" for p in d["prs"]))
        self.assertTrue(all(p["state"] == "merged" for p in d["prs"]))


class A2_Sizes(unittest.TestCase):
    """PR sizes from merge-commit diffs."""

    def test_pr1_size(self) -> None:
        """PR #1 line, file and commit counts match its branch."""
        p = by_number(merge_data())[1]
        self.assertEqual((p["lines"], p["files"], p["commits"]), (110, 2, 2))
        self.assertEqual((p["additions"], p["deletions"]), (110, 0))

    def test_pr3_size(self) -> None:
        """PR #3 counts all 750 changed lines."""
        p = by_number(merge_data())[3]
        self.assertEqual(p["lines"], 750)

    def test_pr4_revert_size(self) -> None:
        """A revert PR counts the reverted lines as deletions."""
        p = by_number(merge_data())[4]
        self.assertEqual((p["additions"], p["deletions"]), (0, 750))


class A3_Timing(unittest.TestCase):
    """PR timings derived from git timestamps."""

    def _branch_times(self, sha: str) -> list[int]:
        """Return the author times of the branch commits merged by merge commit `sha`."""
        out = git_out(merge_repo().path, "log", "--format=%at", f"{sha}^1..{sha}^2")
        return [int(x) for x in out.split()]

    def test_coding_and_lead_match_git(self) -> None:
        """Coding and lead times match the branch and merge commit timestamps."""
        r = merge_repo()
        merges = {}
        for sha, subj in first_parent_shas(r):
            m = re.match(r"Merge pull request #(\d+)", subj)
            if m:
                merges[int(m.group(1))] = sha
        prs = by_number(merge_data())
        for n in (1, 2, 3, 5):  # #4's branch commit is the revert itself
            t = self._branch_times(merges[n])
            ct = int(git_out(r.path, "log", "-1", "--format=%ct", merges[n]))
            self.assertEqual(prs[n]["coding"], max(t) - min(t), n)
            self.assertEqual(prs[n]["lead"], ct - min(t), n)
            self.assertEqual(prs[n]["first_commit"], min(t))
            self.assertEqual(prs[n]["merged"], ct)

    def test_non_negative(self) -> None:
        """No PR duration is negative."""
        for p in merge_data()["prs"]:
            for k in ("coding", "review", "lead", "deploy_wait", "lead_to_deploy", "restore"):
                if p[k] is not None:
                    self.assertGreaterEqual(p[k], 0, (p["number"], k))

    def test_no_api_only_fields_in_git_mode(self) -> None:
        """API-only timings stay null in git mode."""
        for p in merge_data()["prs"]:
            self.assertIsNone(p["pickup"])
            self.assertIsNone(p["open_to_merge"])
            self.assertIsNone(p["created"])
            self.assertIsNone(p["first_review"])

    def test_every_derived_git_timing_is_flagged_inferred(self) -> None:
        """Any duration derived from git-inferred timestamps must be listed in `inferred`.
        lead_to_deploy is computed from the inferred first-commit time, so it must be flagged too
        (otherwise the dashboard's Source column says 'exact')."""
        for p in merge_data()["prs"]:
            for k in ("coding", "review", "lead", "lead_to_deploy"):
                if p[k] is not None:
                    self.assertIn(k, p["inferred"], (p["number"], k))


class A4_Releases(unittest.TestCase):
    """Release detection from tags."""

    def test_both_tags_found(self) -> None:
        """Both release tags are found, oldest first."""
        names = [d["name"] for d in merge_data()["deploys"]]
        self.assertEqual(names, ["v1.0.0", "v1.1.0"])

    def test_pr_release_assignment(self) -> None:
        """Each PR is assigned to the first release that contains it."""
        prs = by_number(merge_data())
        self.assertEqual([prs[i]["release"] for i in range(1, 6)], ["v1.0.0", "v1.0.0", "v1.1.0", "v1.1.0", "v1.1.0"])
        for p in prs.values():
            self.assertIsNotNone(p["deployed"])
            self.assertEqual(p["deploy_wait"], p["deployed"] - p["merged"])

    def test_deploy_fix_flag(self) -> None:
        """A release that ships a hotfix or revert is flagged as a fix."""
        fix = {d["name"]: d["fix"] for d in merge_data()["deploys"]}
        self.assertEqual(fix, {"v1.0.0": False, "v1.1.0": True})

    def test_direct_commit_after_last_tag_has_no_release_pr(self) -> None:
        """A trailing direct commit does not create a PR."""
        # No PR exists for the trailing direct commit, and nothing claims it.
        self.assertEqual(len(merge_data()["prs"]), 5)


class A5_Restore(unittest.TestCase):
    """Time to restore service after a revert."""

    def test_restore_equals_merge_gap(self) -> None:
        """Restore time is the gap between the reverted PR's merge and the revert's merge."""
        prs = by_number(merge_data())
        self.assertEqual(prs[4]["restore"], prs[4]["merged"] - prs[3]["merged"])
        for n in (1, 2, 3, 5):
            self.assertIsNone(prs[n]["restore"])


class A6_Squash(unittest.TestCase):
    """PR inference from a squash-merge history."""

    def test_numbers_and_commits_null(self) -> None:
        """Squash commits yield PRs #2-#8 with no commit count."""
        d = squash_data()
        prs = by_number(d)
        self.assertEqual(sorted(prs), [2, 3, 4, 5, 6, 7, 8])
        self.assertTrue(all(p["commits"] is None for p in prs.values()))
        self.assertTrue(all(p["source"] == "squash-commit" for p in prs.values()))
        self.assertEqual(prs[3]["title"], "refactor: change number 3")

    def test_revert_kind_and_restore(self) -> None:
        """A squash revert is typed as a revert and gets a restore time."""
        prs = by_number(squash_data())
        self.assertEqual(prs[8]["kind"], "revert")
        self.assertEqual(prs[8]["type"], "revert")
        self.assertEqual(prs[8]["restore"], prs[8]["merged"] - prs[2]["merged"])

    def test_no_branch_timings(self) -> None:
        """Squash PRs have no branch-based timings."""
        for p in squash_data()["prs"]:
            self.assertIsNone(p["coding"])
            self.assertIsNone(p["lead"])
            self.assertIsNone(p["first_commit"])

    def test_squash_pr_sizes(self) -> None:
        """Squash PR sizes come from the squash commit's diff."""
        prs = by_number(squash_data())
        self.assertEqual(prs[4]["lines"], 40)
        self.assertEqual(prs[8]["deletions"], 19)  # f2.js 20 lines -> 1 line


class A7_LocSeries(unittest.TestCase):
    """Lines-of-code time series."""

    def _check(self, repo_path: str | Path, *extra: str) -> dict[str, Any]:
        """Collect `repo_path` without default excludes and check the LOC series invariants.

        The last total must equal the real line count, the language series must sum to each
        day's total, and days must be sorted and unique.

        Returns:
            The collected data.
        """
        d, _ = collect(repo_path, "--no-default-excludes", *extra)
        loc = d["loc"]
        self.assertEqual(loc["days"][-1][1], real_line_count(repo_path))
        for i, day in enumerate(loc["days"]):
            self.assertEqual(sum(v[i] for v in loc["langs"].values()), day[1], f"day index {i}")
        days = [x[0] for x in loc["days"]]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(set(days)), len(days))
        return d

    def test_merge_fixture(self) -> None:
        """LOC series invariants hold for the merge-workflow repo."""
        self._check(merge_repo().path)

    def test_squash_fixture(self) -> None:
        """LOC series invariants hold for the squash-workflow repo."""
        self._check(squash_repo().path)

    def test_synthetic_150(self) -> None:
        """LOC series invariants hold for a 150-PR synthetic repo."""
        self._check(helpers.cached_synthetic(150))

    def test_other_bucket_sums(self) -> None:
        """Languages beyond MAX_LANGS are folded into an Other series."""
        r = Repo()
        files = {f"f{i}.ext{i}": lines(i + 1) for i in range(16)}
        r.commit("chore: many languages", files)
        r.commit("fix: more", {"a.py": lines(5)})
        d = self._check(r.path)
        self.assertIn("Other", d["loc"]["langs"])
        self.assertLessEqual(len(d["loc"]["langs"]), rs.MAX_LANGS + 1)

    def test_final_total_with_deletions_and_renames(self) -> None:
        """LOC totals stay correct across renames and deletions."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(30), "b.js": lines(20), "docs/c.md": lines(9)})
        r.git("mv", "a.py", "z.py")
        r.git("rm", "-q", "b.js")
        r.commit("refactor: move and delete", {"z.py": lines(31)})
        self._check(r.path)

    def test_binary_files_ignored(self) -> None:
        """Binary files are not counted as lines of code."""
        r = Repo()
        (r.path / "img.png").write_bytes(b"\x89PNG\0\0\0" * 100)
        r.commit("feat: add", {"a.py": lines(10)})
        d = self._check(r.path)
        self.assertEqual(d["loc"]["days"][-1][1], 10)

    def test_default_excludes_reduce_total(self) -> None:
        """Default excludes drop lock files and vendored code from LOC."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(10), "package-lock.json": lines(500), "vendor/x.js": lines(100)})
        full, _ = collect(r, "--no-default-excludes")
        dflt, _ = collect(r)
        self.assertEqual(full["loc"]["days"][-1][1], 610)
        self.assertEqual(dflt["loc"]["days"][-1][1], 10)

    def test_test_vs_non_test_split(self) -> None:
        """Each day carries cumulative test lines, for all languages and for code languages only."""
        r = Repo()
        r.commit("chore: init", {"src/a.py": lines(10), "tests/test_a.py": lines(4), "tests/data.json": lines(3)})
        r.advance(DAY)
        r.commit("test: more", {"tests/test_a.py": lines(6), "b_test.go": lines(5), "README.md": lines(2)})
        r.advance(DAY)
        r.git("rm", "-q", "tests/data.json")
        r.commit("test: drop data", {"tests/test_a.py": lines(1)})
        d = self._check(r.path)
        self.assertEqual([day[4:] for day in d["loc"]["days"]], [[7, 4], [14, 11], [6, 6]])


class A8_Excludes(unittest.TestCase):
    """Path excludes for PR sizes and LOC."""

    @classmethod
    def setUpClass(cls) -> None:
        """Build a repo whose PRs touch lock, vendored, generated and minified files."""
        r = Repo()
        r.commit("chore: initial commit", {"src/app.py": lines(5)})
        r.advance(DAY)
        r.squash_pr(1, "feat: add stuff", {
            "src/app.py": lines(15), "package-lock.json": lines(1000), "vendor/x.js": lines(500),
            "dist/bundle.js": lines(40), "web/a.min.js": lines(30), "go.sum": lines(20),
            "node_modules/q/i.js": lines(7), "yarn.lock": lines(10)})
        r.advance(DAY)
        r.merge_pr(2, "feat/more", "feat: more", [("more", {"lib/b.py": lines(8), "package-lock.json": lines(1001)})])
        cls.r = r

    def test_default_excludes_pr_size_and_files(self) -> None:
        """Default excludes leave only real source in PR sizes and file counts."""
        d, _ = collect(self.r)
        prs = by_number(d)
        self.assertEqual((prs[1]["lines"], prs[1]["files"]), (10, 1))   # app.py 5 -> 15 rewrites 10 added lines
        self.assertEqual(prs[2]["lines"], 8)

    def test_default_excludes_loc(self) -> None:
        """Default excludes apply to the LOC total."""
        d, _ = collect(self.r)
        self.assertEqual(d["loc"]["days"][-1][1], 15 + 8)

    def test_no_default_excludes(self) -> None:
        """--no-default-excludes counts every file."""
        d, _ = collect(self.r, "--no-default-excludes")
        prs = by_number(d)
        self.assertEqual(prs[1]["lines"], 10 + 1000 + 500 + 40 + 30 + 20 + 7 + 10)
        self.assertEqual(d["loc"]["days"][-1][1], real_line_count(self.r.path))
        self.assertEqual(d["meta"]["excludes"], [])

    def test_custom_exclude_regex(self) -> None:
        """--exclude regexes remove matching paths from sizes and LOC."""
        d, _ = collect(self.r, "--exclude", r"^lib/", "--exclude", r"app\.py$")
        prs = by_number(d)
        self.assertEqual(prs[1]["lines"], 0)
        self.assertEqual(prs[2]["lines"], 0)
        self.assertEqual(d["loc"]["days"][-1][1], 0)

    def test_custom_exclude_combines_with_defaults(self) -> None:
        """--exclude adds to the default excludes instead of replacing them."""
        d, _ = collect(self.r, "--exclude", r"^lib/")
        self.assertEqual(by_number(d)[2]["lines"], 0)
        self.assertEqual(by_number(d)[1]["lines"], 10)


class A9_SyncMerges(unittest.TestCase):
    """Branch sync merges are not mistaken for PRs."""

    def test_sync_merge_not_a_pr(self) -> None:
        """A 'Merge branch main of ...' sync merge is counted as a sync, not a PR."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(5)})
        r.merge_pr(1, "feat/x", "feat: x", [("x", {"x.py": lines(10)})])
        base = r.git("rev-parse", "HEAD")
        r.git("checkout", "-q", "-b", "side", base)
        r.commit("remote work", {"side.py": lines(3)})
        r.git("checkout", "-q", "main")
        r.commit("local work", {"local.py": lines(3)})
        r.git("merge", "--no-ff", "-q", "side", "-m", "Merge branch 'main' of github.com:acme/widgets")
        d, _ = collect(r)
        self.assertEqual([p["number"] for p in d["prs"]], [1])
        self.assertEqual(d["meta"]["sync_merges"], 1)

    def test_sync_merge_when_default_branch_is_not_main_or_master(self) -> None:
        """Sync merges are recognised when the default branch is neither main nor master.

        No origin/HEAD and a default branch called 'trunk': detect_branch falls back to 'HEAD', so the
        'Merge branch 'trunk' of ...' sync merge is no longer recognised and shows up as a bogus PR.
        """
        r = Repo()
        r.git("branch", "-m", "trunk")
        r.commit("chore: initial commit", {"a.py": lines(5)})
        r.git("checkout", "-q", "-b", "side")
        r.commit("remote work", {"side.py": lines(3)})
        r.git("checkout", "-q", "trunk")
        r.commit("local work", {"local.py": lines(3)})
        r.git("merge", "--no-ff", "-q", "side", "-m", "Merge branch 'trunk' of github.com:acme/widgets")
        d, _ = collect(r)
        self.assertEqual(d["prs"], [], "sync merge must not be counted as a PR")
        self.assertEqual(d["meta"]["sync_merges"], 1)

    def test_parse_message_sync_variants(self) -> None:
        """parse_message() flags sync-merge subjects and leaves feature merges alone."""
        pm = rs.parse_message
        for subj in ("Merge branch 'main' of github.com:acme/widgets", "Merge branch 'main' of https://host/acme/w",
                     "Merge remote-tracking branch 'origin/main'", "Merge branch 'main' into feature",
                     "Merge remote-tracking branch 'origin/main' into feat-x"):
            self.assertTrue(pm(subj, "", "origin/main")["sync"], subj)
            self.assertTrue(pm(subj, "", "main")["sync"], subj)
        self.assertFalse(pm("Merge branch 'feature/x'", "", "origin/main")["sync"])
        self.assertEqual(pm("Merge branch 'feature/x'", "", "origin/main")["branch"], "feature/x")
        self.assertFalse(pm("Merge branch 'main-fix' of host:p", "", "origin/main")["sync"])
        self.assertTrue(pm("Merge branch 'release/1.0' of host:p", "", "origin/release/1.0")["sync"])


class A10_MessageStyles(unittest.TestCase):
    """Merge and squash message formats of each hosting service."""

    def test_github(self) -> None:
        """GitHub merge messages give the number, title and branch."""
        m = rs.parse_message("Merge pull request #7 from org/feat/x", "Title here\n\nmore", "origin/main")
        self.assertEqual((m["number"], m["title"], m["branch"]), (7, "Title here", "feat/x"))

    def test_github_empty_body(self) -> None:
        """A GitHub merge with no body has a number but no title."""
        m = rs.parse_message("Merge pull request #7 from org/feat/x", "", "origin/main")
        self.assertEqual(m["number"], 7)
        self.assertIsNone(m["title"])

    def test_gitlab(self) -> None:
        """GitLab merge messages take the MR number from 'See merge request'."""
        m = rs.parse_message("Merge branch 'feat-x' into 'main'", "Add x\n\nSee merge request grp/sub/proj!12", "main")
        self.assertEqual((m["number"], m["title"], m["branch"]), (12, "Add x", "feat-x"))

    def test_gitlab_with_extra_lines(self) -> None:
        """GitLab merge bodies with extra trailer lines still parse."""
        m = rs.parse_message("Merge branch 'feat-x' into 'main'", "Add x\n\nCloses #5\n\nSee merge request g/p!120", "main")
        self.assertEqual((m["number"], m["title"]), (120, "Add x"))

    def test_gitlab_no_title(self) -> None:
        """A GitLab merge body without a title line gives no title."""
        m = rs.parse_message("Merge branch 'feat-x' into 'main'", "See merge request g/p!13", "main")
        self.assertEqual(m["number"], 13)
        self.assertIsNone(m["title"])

    def test_bitbucket(self) -> None:
        """Bitbucket 'Merged in ... (pull request #N)' messages parse."""
        m = rs.parse_message("Merged in feature/foo (pull request #34)", "Add foo\n\nApproved-by: Someone", "main")
        self.assertEqual((m["number"], m["title"], m["branch"]), (34, "Add foo", "feature/foo"))

    def test_squash(self) -> None:
        """Squash subjects ending in (#N) parse, including nested references."""
        m = rs.parse_message("Fix the thing (#123)", "body", "main")
        self.assertEqual((m["number"], m["title"]), (123, "Fix the thing"))
        m = rs.parse_message('Revert "Fix (#12)" (#13)', "", "main")
        self.assertEqual((m["number"], m["title"]), (13, 'Revert "Fix (#12)"'))

    def test_plain_commit_not_pr(self) -> None:
        """A commit that mentions #N mid-subject is not a PR."""
        m = rs.parse_message("fix: something (see #5) now", "", "main")
        self.assertIsNone(m["number"])
        self.assertFalse(m["has_ref"])

    def test_integration_gitlab_and_bitbucket_merges(self) -> None:
        """GitLab and Bitbucket merges are found in a real repo and typed by title or branch."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(5)})

        def merge(br: str, msg_args: Sequence[str], files: Mapping[str, str]) -> None:
            """Commit `files` on branch `br`, merge it into main with git merge `msg_args`, then delete the branch."""
            r.git("checkout", "-q", "-b", br)
            r.commit("work", files)
            r.git("checkout", "-q", "main")
            r.git("merge", "--no-ff", "-q", br, *msg_args)
            r.git("branch", "-q", "-D", br)
            r.advance(3600)

        merge("fix-bug", ["-m", "Merge branch 'fix-bug' into 'main'", "-m", "fix: squash the bug\n\nSee merge request grp/proj!12"],
              {"b.py": lines(3)})
        merge("feature/foo", ["-m", "Merged in feature/foo (pull request #34)", "-m", "Add foo"], {"c.py": lines(4)})
        d, _ = collect(r)
        prs = by_number(d)
        self.assertEqual(sorted(prs), [12, 34])
        self.assertEqual(prs[12]["title"], "fix: squash the bug")
        self.assertEqual(prs[12]["type"], "fix")
        self.assertEqual(prs[34]["title"], "Add foo")
        self.assertEqual(prs[34]["type"], "feat")  # from branch name feature/foo
        self.assertEqual(prs[34]["type_source"], "branch")


class A11_Options(unittest.TestCase):
    """Command-line options in git mode."""

    def test_since_until(self) -> None:
        """--since and --until filter PRs and releases by day."""
        d, _ = collect(merge_repo(), "--since", "2022-01-05", "--until", "2022-01-05")
        self.assertEqual([p["number"] for p in d["prs"]], [3])  # #3 merged 2022-01-05 13:00Z
        d, _ = collect(merge_repo(), "--since", "2022-01-05")
        self.assertEqual([p["number"] for p in d["prs"]], [3, 4, 5])
        self.assertEqual([x["name"] for x in d["deploys"]], ["v1.1.0"])
        d, _ = collect(merge_repo(), "--until", "2022-01-04")
        self.assertEqual([p["number"] for p in d["prs"]], [1, 2])
        self.assertEqual([x["name"] for x in d["deploys"]], ["v1.0.0"])

    def test_until_is_inclusive_of_that_day_only(self) -> None:
        """--until includes its whole day and nothing after it."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(2)})
        r.clock = T0 + 5 * DAY  # exactly 2022-01-06T00:00:00Z
        r.squash_pr(1, "feat: edge", {"b.py": lines(3)})
        d, _ = collect(r, "--until", "2022-01-05")
        self.assertEqual(d["prs"], [], "a PR merged at 2022-01-06T00:00:00Z is after --until 2022-01-05")
        d, _ = collect(r, "--until", "2022-01-06")
        self.assertEqual(len(d["prs"]), 1)

    def test_month_and_year_windows(self) -> None:
        """YYYY-MM and YYYY cover whole UTC months and years: --until includes the last second, not the next."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(2)})
        for n, when in ((1, T0 + 30 * DAY + DAY - 1),   # 2022-01-31T23:59:59Z
                        (2, T0 + 31 * DAY),             # 2022-02-01T00:00:00Z
                        (3, T0 + 365 * DAY - 1),        # 2022-12-31T23:59:59Z
                        (4, T0 + 365 * DAY)):           # 2023-01-01T00:00:00Z
            r.clock = when
            r.squash_pr(n, f"feat: pr {n}", {f"f{n}.py": lines(2)})
        nums = lambda *a: [p["number"] for p in collect(r, *a)[0]["prs"]]  # noqa: E731
        self.assertEqual(nums("--until", "2022-01"), [1])
        self.assertEqual(nums("--since", "2022-02"), [2, 3, 4])
        self.assertEqual(nums("--since", "2022-02", "--until", "2022-12"), [2, 3])
        self.assertEqual(nums("--since", "2022", "--until", "2022"), [1, 2, 3])
        self.assertEqual(nums("--since", "2023"), [4])
        self.assertEqual(nums("--since", "2022-01-31", "--until", "2022-02-01"), [1, 2])

    def test_durations_count_back_from_now(self) -> None:
        """90d, 6w, 6m and 1y count back from now in UTC; months clamp to the month's last day."""
        now = rs.dt.datetime(2025, 3, 31, 12, tzinfo=rs.dt.timezone.utc)
        iso = lambda v: rs.dt.datetime.fromtimestamp(rs.parse_when(v, "--since", False, now), rs.dt.timezone.utc).isoformat()  # noqa: E731
        self.assertEqual(iso("1y"), "2024-03-31T12:00:00+00:00")
        self.assertEqual(iso("1m"), "2025-02-28T12:00:00+00:00")
        self.assertEqual(iso("13m"), "2024-02-29T12:00:00+00:00")
        self.assertEqual(iso("6w"), "2025-02-17T12:00:00+00:00")
        self.assertEqual(iso("90d"), "2024-12-31T12:00:00+00:00")
        self.assertEqual(iso("1Y"), iso("12m"))
        self.assertEqual(rs.parse_when("2024-02", "--until", True, now), rs.parse_when("2024-03", "--since", False, now))

    def test_relative_window_in_a_run(self) -> None:
        """--since 1y drops PRs from 2022; a long enough duration keeps them all; --until 1d keeps them too."""
        d, _ = collect(merge_repo(), "--since", "1y")
        self.assertEqual(d["prs"], [])
        self.assertEqual(d["deploys"], [])
        d, _ = collect(merge_repo(), "--since", "100y", "--until", "1d")
        self.assertEqual([p["number"] for p in d["prs"]], [1, 2, 3, 4, 5])

    def test_since_must_be_before_until(self) -> None:
        """An empty or reversed window is an error, not an empty dashboard."""
        for since, until in (("2025-07", "2025-01"), ("2025-03-01", "2025-02-28"), ("2025-08", "2025-07")):
            with self.assertRaises(rs.ToolError, msg=(since, until)):
                collect(merge_repo(), "--since", since, "--until", until)
        collect(merge_repo(), "--since", "2025-07", "--until", "2025-07")  # one whole month is fine

    def test_since_excludes_earlier_day(self) -> None:
        """--since drops PRs merged the day before."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(2)})
        r.clock = T0 + 5 * DAY - 1
        r.squash_pr(1, "feat: late", {"b.py": lines(3)})
        d, _ = collect(r, "--since", "2022-01-06")
        self.assertEqual(d["prs"], [])

    def _bot_repo(self) -> Repo:
        """Build a repo with one human squash PR and two from bot-like committers."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(2)})
        r.squash_pr(1, "feat: human change", {"h.py": lines(5)})
        r.git("config", "user.name", "dependabot[bot]")
        r.squash_pr(2, "chore: bump dep", {"d.py": lines(5)})
        r.git("config", "user.name", "renovate-bot")
        r.squash_pr(3, "chore: bump other", {"e.py": lines(5)})
        r.git("config", "user.name", "Dev One")
        return r

    def test_bot_detection_and_exclusion(self) -> None:
        """Bot authors are flagged and dropped by --exclude-bots."""
        r = self._bot_repo()
        d, _ = collect(r)
        self.assertEqual({p["number"]: p["bot"] for p in d["prs"]}, {1: False, 2: True, 3: True})
        d, _ = collect(r, "--exclude-bots")
        self.assertEqual([p["number"] for p in d["prs"]], [1])

    def test_bot_pattern(self) -> None:
        """--bot-pattern replaces the default bot detection."""
        r = self._bot_repo()
        d, _ = collect(r, "--bot-pattern", r"^Dev One$")
        self.assertEqual({p["number"]: p["bot"] for p in d["prs"]}, {1: True, 2: False, 3: False})
        d, _ = collect(r, "--bot-pattern", r"^Dev One$", "--exclude-bots")
        self.assertEqual([p["number"] for p in d["prs"]], [2, 3])

    def test_no_titles(self) -> None:
        """--no-titles hides titles but still classifies PRs by them."""
        d, _ = collect(merge_repo(), "--no-titles")
        self.assertEqual([p["title"] for p in d["prs"]], ["#1", "#2", "#3", "#4", "#5"])
        # classification still uses the real titles
        self.assertEqual(by_number(d)[3]["type"], "feat")
        self.assertTrue(by_number(d)[3]["breaking"])

    def test_no_titles_leaks_nothing_in_outputs(self) -> None:
        """--no-titles keeps titles, commit messages and branch names out of every output."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(2)})
        r.merge_pr(1, "feat/zebrafish-branch", "feat: SecretTitleWord launch", [("wip SecretCommitWord", {"b.py": lines(3)})])
        out = tmpdir()
        p = run_cli([r.path, "-o", out / "o.html", "--json", out / "o.json", "--csv", out / "o.csv", "--no-titles"])
        self.assertEqual(p.returncode, 0, p.stderr)
        for f in ("o.html", "o.json", "o.csv"):
            text = (out / f).read_text()
            for word in ("SecretTitleWord", "SecretCommitWord", "zebrafish"):
                self.assertNotIn(word, text, f"{word} leaked into {f}")

    def test_commits_as_prs(self) -> None:
        """--commits-as-prs turns direct commits into PRs keyed by short SHA."""
        d, _ = collect(merge_repo(), "--commits-as-prs")
        self.assertEqual(len(d["prs"]), 7)
        self.assertEqual(d["meta"]["direct_commits"], 0)
        direct = [p for p in d["prs"] if p["number"] is None]
        self.assertEqual(len(direct), 2)
        for p in direct:
            self.assertEqual(len(str(p["id"])), 8)

    def test_commits_as_prs_on_prless_repo(self) -> None:
        """--commits-as-prs gives a repo without PRs one PR per commit."""
        r = Repo()
        for i in range(4):
            r.commit(f"feat: change {i}", {f"f{i}.py": lines(5)})
        d, _ = collect(r)
        self.assertEqual(d["prs"], [])
        self.assertTrue(any("No pull requests" in w for w in d["meta"]["warnings"]))
        d, _ = collect(r, "--commits-as-prs")
        self.assertEqual(len(d["prs"]), 4)

    def test_deploy_source_merges(self) -> None:
        """--deploy-source merges treats every merge as a deploy."""
        d, err = collect(merge_repo(), "--deploy-source", "merges")
        self.assertEqual(d["deploys"], [])
        self.assertEqual(d["meta"]["deploy_source"], "merges")
        for p in d["prs"]:
            self.assertEqual(p["deployed"], p["merged"])
            self.assertEqual(p["deploy_wait"], 0)
        self.assertFalse(any("release tags" in w for w in d["meta"]["warnings"]))

    def test_tag_pattern(self) -> None:
        """--tag-pattern selects which tags count as releases."""
        d, _ = collect(merge_repo(), "--tag-pattern", r"^v1\.1")
        self.assertEqual([x["name"] for x in d["deploys"]], ["v1.1.0"])
        self.assertTrue(all(p["release"] == "v1.1.0" for p in d["prs"]))
        d, _ = collect(merge_repo(), "--tag-pattern", r"^nomatch$")
        self.assertEqual(d["deploys"], [])
        self.assertTrue(any("release tags" in w for w in d["meta"]["warnings"]))

    def test_branch_option(self) -> None:
        """--branch analyses another branch and rejects unknown ones cleanly."""
        r = fixtures.merge_workflow_repo()
        sha2 = [s for s, subj in first_parent_shas(r) if subj.startswith("Merge pull request #2")][0]
        r.git("branch", "release/1.0", sha2)
        d, _ = collect(r, "--branch", "release/1.0")
        self.assertEqual([p["number"] for p in d["prs"]], [1, 2])
        self.assertEqual(d["meta"]["branch"], "release/1.0")
        self.assertEqual([x["name"] for x in d["deploys"]], ["v1.0.0"])
        p = run_cli([r.path, "-o", tmpdir() / "o.html", "--branch", "nope"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("error:", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_failure_pattern(self) -> None:
        """--failure-pattern decides which PRs count as hotfixes."""
        d, _ = collect(merge_repo(), "--failure-pattern", r"correct typo")
        self.assertEqual(by_number(d)[2]["kind"], "hotfix")
        self.assertIsNone(by_number(d)[5]["kind"])

    def test_max_prs_git_mode(self) -> None:
        """--max-prs keeps the newest PRs in git mode."""
        d, _ = collect(merge_repo(), "--max-prs", "2")
        self.assertEqual([p["number"] for p in d["prs"]], [4, 5])


class A12_EdgeRepos(unittest.TestCase):
    """Unusual repositories and paths."""

    def test_empty_repo(self) -> None:
        """A repo with no commits exits 1 with a clean error."""
        r = Repo()
        p = run_cli([r.path, "-o", tmpdir() / "o.html"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("error:", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_non_git_dir(self) -> None:
        """A directory that is not a git repo exits 1 with a clean error."""
        d = tmpdir()
        p = run_cli([d, "-o", d / "o.html"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("not a git repository", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_missing_dir(self) -> None:
        """A missing directory exits 1 without a traceback."""
        p = run_cli([tmpdir() / "does-not-exist", "-o", tmpdir() / "o.html"])
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_unwritable_output_is_clean_error(self) -> None:
        """An unwritable output path exits 1 without a traceback."""
        p = run_cli([merge_repo().path, "-o", "/nonexistent-dir-xyz/o.html"])
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 1)

    def test_shallow_clone_warning(self) -> None:
        """A shallow clone produces a warning."""
        dest = tmpdir() / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "2", f"file://{merge_repo().path}", str(dest)], check=True,
                       capture_output=True)
        d, _ = collect(dest)
        self.assertTrue(any("shallow" in w.lower() for w in d["meta"]["warnings"]), d["meta"]["warnings"])
        self.assertEqual(d["meta"]["branch"], "origin/main")

    def test_full_clone_uses_origin_head(self) -> None:
        """A full clone analyses the branch origin/HEAD points to."""
        dest = tmpdir() / "clone"
        subprocess.run(["git", "clone", "-q", f"file://{merge_repo().path}", str(dest)], check=True, capture_output=True)
        d, _ = collect(dest)
        self.assertEqual(d["meta"]["branch"], "origin/main")
        self.assertEqual([p["number"] for p in d["prs"]], [1, 2, 3, 4, 5])
        self.assertEqual([x["name"] for x in d["deploys"]], ["v1.0.0", "v1.1.0"])

    def test_no_tags(self) -> None:
        """A repo without tags warns and leaves releases empty."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(3)})
        r.squash_pr(1, "feat: x", {"b.py": lines(3)})
        d, _ = collect(r)
        self.assertEqual(d["deploys"], [])
        self.assertTrue(any("release tag" in w for w in d["meta"]["warnings"]))
        self.assertIsNone(d["prs"][0]["release"])
        self.assertIsNone(d["prs"][0]["deployed"])

    def test_zero_prs_renders(self) -> None:
        """A repo with no PRs still renders a dashboard, with a warning."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(3)})
        r.commit("fix: direct", {"a.py": lines(4)})
        out = tmpdir()
        p = run_cli([r.path, "-o", out / "o.html"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("warning: No pull requests", p.stderr)
        d = extract_data((out / "o.html").read_text())
        self.assertEqual(d["prs"], [])
        self.assertGreater(len(d["loc"]["days"]), 0)

    def test_detached_head_without_branches(self) -> None:
        """A detached HEAD with no main or master branch analyses HEAD."""
        r = fixtures.merge_workflow_repo()
        r.git("branch", "-m", "trunk")
        r.git("checkout", "-q", "--detach")
        d, _ = collect(r)
        self.assertEqual(d["meta"]["branch"], "HEAD")
        self.assertEqual(len(d["prs"]), 5)

    def test_branch_with_slash_name(self) -> None:
        """Branch names containing a slash work with --branch."""
        r = Repo()
        r.git("checkout", "-q", "-b", "release/2.x")
        r.commit("chore: init", {"a.py": lines(3)})
        r.git("checkout", "-q", "-b", "feat/x")
        r.commit("x", {"b.py": lines(2)})
        r.git("checkout", "-q", "release/2.x")
        r.git("merge", "--no-ff", "-q", "feat/x", "-m", "Merge pull request #9 from dev/feat/x", "-m", "feat: x")
        d, _ = collect(r, "--branch", "release/2.x")
        self.assertEqual([p["number"] for p in d["prs"]], [9])
        self.assertEqual(d["meta"]["branch"], "release/2.x")

    def test_master_branch_autodetected(self) -> None:
        """A master branch is detected when there is no main."""
        r = Repo()
        r.git("branch", "-m", "master")
        r.commit("chore: init", {"a.py": lines(3)})
        r.squash_pr(1, "feat: x", {"b.py": lines(3)})
        d, _ = collect(r)
        self.assertEqual(d["meta"]["branch"], "master")

    def test_single_pr_repo(self) -> None:
        """A repo with a single PR is handled."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(3)})
        r.squash_pr(1, "feat: only", {"b.py": lines(30)})
        d, _ = collect(r)
        self.assertEqual(len(d["prs"]), 1)


class A13_Unicode(unittest.TestCase):
    """Non-ASCII paths and titles."""

    def test_unicode_paths_and_titles(self) -> None:
        """Unicode and unusual file names and titles survive all three outputs."""
        r = Repo()
        r.commit("chore: initial commit", {"src/日本語/ファイル.py": lines(4), "docs/é à.md": lines(3), "🚀.js": lines(2),
                                           'we"ird name.py': lines(5), "tab\tname.py": lines(6)})
        r.merge_pr(1, "feat/ünï-🚀", "feat: ünïcode 🚀 titre «x» 日本語", [("wip é", {"src/日本語/新しい.py": lines(7)})])
        r.squash_pr(2, "fix: ñandú ✓", {"src/ñ/ü.py": lines(2)})
        out = tmpdir()
        p = run_cli([r.path, "-o", out / "o.html", "--json", out / "o.json", "--csv", out / "o.csv", "--no-default-excludes"])
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads((out / "o.json").read_text(encoding="utf-8"))
        prs = by_number(d)
        self.assertEqual(prs[1]["title"], "feat: ünïcode 🚀 titre «x» 日本語")
        self.assertEqual(prs[1]["files"], 1)
        self.assertEqual(prs[2]["title"], "fix: ñandú ✓")
        self.assertEqual(d["loc"]["days"][-1][1], real_line_count(r.path))
        self.assertEqual(extract_data((out / "o.html").read_text(encoding="utf-8"))["prs"][0]["title"], prs[1]["title"])
        self.assertGreater(len((out / "o.csv").read_text(encoding="utf-8").splitlines()), 2)


class A14_Performance(unittest.TestCase):
    """Run-time budgets for large histories."""

    def test_400_prs_under_20_seconds(self) -> None:
        """collect() handles a 400-PR repo in under 20 seconds."""
        path = helpers.fast_import_repo(400)
        t = time.time()
        d, _ = collect(path)
        elapsed = time.time() - t
        print(f"\n  [perf] collect() on a 400-PR merge-style repo: {elapsed:.2f}s", file=sys.stderr)
        self.assertEqual(len(d["prs"]), 400)
        self.assertLess(elapsed, 20)

    def test_synthetic_150_collect_and_render(self) -> None:
        """Collecting and rendering 150 PRs is fast and the HTML stays under 2 MB."""
        path = helpers.cached_synthetic(150)
        t = time.time()
        d, _ = collect(path)
        html = rs.render_html(d)
        elapsed = time.time() - t
        print(f"\n  [perf] collect()+render on synthetic_repo(150): {elapsed:.2f}s, html {len(html)//1024} KiB", file=sys.stderr)
        self.assertEqual(len(d["prs"]), 150)
        self.assertLess(elapsed, 20)
        self.assertLess(len(html), 2_000_000)


class A_SyntheticSanity(unittest.TestCase):
    """Invariants over a larger history, for both merge and squash styles."""

    def _check(self, d: dict[str, Any]) -> None:
        """Assert per-PR risk, size and timing invariants plus PR order and uniqueness."""
        for p in d["prs"]:
            self.assertGreaterEqual(p["risk"], 0)
            self.assertLessEqual(p["risk"], 100)
            self.assertIn(p["risk_level"], ("low", "medium", "high", "critical"))
            self.assertEqual(p["risk"], min(100, sum(pts for _, pts in p["risk_factors"])))
            self.assertEqual(p["lines"], p["additions"] + p["deletions"])
            for k in ("coding", "review", "lead", "deploy_wait", "lead_to_deploy", "restore"):
                if p[k] is not None:
                    self.assertGreaterEqual(p[k], 0, (p["number"], k))
        merged = [p["merged"] for p in d["prs"]]
        self.assertEqual(merged, sorted(merged))
        nums = [p["number"] for p in d["prs"]]
        self.assertEqual(len(nums), len(set(nums)))

    def test_merge_style(self) -> None:
        """Invariants hold for a merge-style synthetic history."""
        d, _ = collect(helpers.cached_synthetic(150))
        self._check(d)
        self.assertGreater(len(d["deploys"]), 5)
        self.assertTrue(any(p["kind"] == "revert" and p["restore"] is not None for p in d["prs"]))

    def test_squash_style(self) -> None:
        """Invariants hold for a squash-style synthetic history."""
        d, _ = collect(helpers.cached_synthetic(150, "squash"))
        self._check(d)
        self.assertTrue(all(p["commits"] is None for p in d["prs"] if p["source"] == "squash-commit"))
        self.assertGreater(sum(p["source"] == "squash-commit" for p in d["prs"]), 120)
        self.assertEqual(len(d["prs"]), 150)


# =====================================================================================================
# B. API clients against local mock servers
# =====================================================================================================

def api_repo(url: str = "https://github.com/acme/widgets.git") -> Repo:
    """Build a fresh merge-workflow repo with `url` as its origin remote."""
    r = fixtures.merge_workflow_repo()
    add_remote(r, url)
    return r


def merge_shas(r: Repo) -> dict[int, str]:
    """Map PR number to SHA for the 'Merge pull request #N' commits on main."""
    out = {}
    for sha, subj in first_parent_shas(r):
        m = re.match(r"Merge pull request #(\d+)", subj)
        if m:
            out[int(m.group(1))] = sha
    return out


BASE = T0 + 30 * DAY  # PR creation times in mocks


def github_nodes(r: Repo) -> dict[int, dict[str, Any]]:
    """Build mock GitHub PR nodes #1-#10; the merged ones point at `r`'s real merge commits.

    Besides the five local PRs there is an open PR, a closed one, one into another base branch,
    a bot PR and one reviewed only by its author.
    """
    sh = merge_shas(r)
    h = 3600
    nodes = {}
    c = lambda n: BASE + n * DAY  # noqa: E731
    nodes[1] = ms.gh_node(1, "feat(auth): add login", c(1), merged=c(1) + 3 * h + 20000, first_commit=c(1) - 2 * h,
                          ready=c(1) + h, additions=999, deletions=1, files=7, commits=3, merge_oid=sh[1],
                          reviews=[("alice", "User", "COMMENTED", c(1) + h + 60),            # author
                                   ("helper[bot]", "Bot", "COMMENTED", c(1) + h + 120),      # bot
                                   ("carol", "User", "PENDING", c(1) + h + 180),             # pending
                                   ("bob", "User", "APPROVED", c(1) + h + 2 * h)],
                          labels=["bug"])
    nodes[2] = ms.gh_node(2, "fix: correct typo", c(2), merged=c(2) + 5 * h, merge_oid=sh[2],
                          reviews=[("bob", "User", "APPROVED", c(2) + h)])
    nodes[3] = ms.gh_node(3, "feat!: rework engine", c(3), merged=c(3) + 9 * h, merge_oid=sh[3],
                          reviews=[("bob", "User", "CHANGES_REQUESTED", c(3) + h)])
    nodes[4] = ms.gh_node(4, 'Revert "feat!: rework engine"', c(4), merged=c(4) + 2 * h, merge_oid=sh[4],
                          reviews=[("bob", "User", "APPROVED", c(4) + h)])
    nodes[5] = ms.gh_node(5, "fix: hotfix crash on start", c(5), merged=c(5) + h, merge_oid=sh[5],
                          reviews=[("bob", "User", "APPROVED", c(5) + 60)])
    nodes[6] = ms.gh_node(6, "feat: still open", c(6))
    nodes[7] = ms.gh_node(7, "chore: closed unmerged", c(7), closed=c(7) + h, state="CLOSED")
    nodes[8] = ms.gh_node(8, "feat: other base", c(8), merged=c(8) + h, base="release/9")
    nodes[9] = ms.gh_node(9, "chore: bump deps", c(9), merged=c(9) + h, author="dependabot[bot]", author_type="Bot",
                          reviews=[("bob", "User", "APPROVED", c(9) + 60)])
    nodes[10] = ms.gh_node(10, "fix: no review at all", c(10), merged=c(10) + h, author="erin", additions=300, deletions=50, files=3,
                           reviews=[("erin", "User", "COMMENTED", c(10) + 60)])
    return nodes


def desc_nodes(nodes: Mapping[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the nodes newest first by PR number, the order the GitHub API uses."""
    return [nodes[k] for k in sorted(nodes, reverse=True)]


class B1_GitHub(unittest.TestCase):
    """GitHub GraphQL client against a mock server."""

    @classmethod
    def setUpClass(cls) -> None:
        """Start a mock GitHub with ten PRs and collect the repo through it once."""
        cls.r = api_repo()
        cls.nodes = github_nodes(cls.r)
        cls.srv = ms.MockServer(ms.github_handler(desc_nodes(cls.nodes), page_size=3, expect_token=FAKE)).start()
        cls.data, cls.err = collect(cls.r, "--fake-api", cls.srv.url, env={"GITHUB_TOKEN": FAKE})
        cls.prs = by_number(cls.data)

    @classmethod
    def tearDownClass(cls) -> None:
        """Stop the mock server."""
        cls.srv.stop()

    def test_mode_and_pagination(self) -> None:
        """API mode pages through all PRs using GraphQL cursors."""
        self.assertEqual(self.data["meta"]["mode"], "api")
        self.assertEqual(self.data["meta"]["provider"], "github")
        self.assertEqual(len(self.srv.requests), 4)  # 10 nodes / 3 per page
        cursors = [json.loads(q["body"])["variables"].get("cursor") for q in self.srv.requests]
        self.assertEqual(cursors, [None, "3", "6", "9"])

    def test_base_branch_filter(self) -> None:
        """PRs into other base branches are dropped."""
        self.assertNotIn(8, self.prs)
        self.assertEqual(sorted(self.prs), [1, 2, 3, 4, 5, 6, 7, 9, 10])

    def test_all_bases(self) -> None:
        """--all-bases keeps PRs into other base branches."""
        with ms.MockServer(ms.github_handler(desc_nodes(self.nodes), page_size=3)) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, "--all-bases", env={"GITHUB_TOKEN": FAKE})
        self.assertIn(8, by_number(d))

    def test_states(self) -> None:
        """Open, closed and merged states map correctly."""
        self.assertEqual(self.prs[6]["state"], "open")
        self.assertIsNone(self.prs[6]["merged"])
        self.assertEqual(self.prs[7]["state"], "closed")
        self.assertIsNone(self.prs[7]["merged"])
        self.assertEqual(self.prs[1]["state"], "merged")

    def test_first_review_ignores_author_bot_pending(self) -> None:
        """First review ignores the author, bots and pending reviews."""
        p = self.prs[1]
        c1, h = BASE + DAY, 3600
        self.assertEqual(p["first_review"], c1 + 3 * h)  # bob, not alice/helper[bot]/carol(PENDING)
        self.assertTrue(p["reviewed"])

    def test_ready_time_from_event(self) -> None:
        """Ready-for-review time comes from the timeline event and drives the timings."""
        c1, h = BASE + DAY, 3600
        p = self.prs[1]
        self.assertEqual(p["ready"], c1 + h)
        self.assertEqual(p["created"], c1)
        self.assertEqual(p["coding"], 3 * h)               # first commit (c1-2h) -> ready (c1+1h)
        self.assertEqual(p["pickup"], 2 * h)               # ready -> first review
        self.assertEqual(p["review"], p["merged"] - p["first_review"])
        self.assertEqual(p["open_to_merge"], p["merged"] - c1)
        self.assertEqual(p["lead"], p["merged"] - (c1 - 2 * h))

    def test_ready_defaults_to_created(self) -> None:
        """Ready time defaults to creation time without a ready event."""
        self.assertEqual(self.prs[2]["ready"], self.prs[2]["created"])

    def test_bot_flag(self) -> None:
        """Bot-authored PRs are flagged."""
        self.assertTrue(self.prs[9]["bot"])
        self.assertFalse(self.prs[1]["bot"])

    def test_unreviewed_risk_rule(self) -> None:
        """Merged PRs with no outside review get the unreviewed risk factor."""
        p = self.prs[10]
        self.assertFalse(p["reviewed"])
        self.assertIn(["unreviewed", 10], p["risk_factors"])
        self.assertNotIn("unreviewed", [f[0] for f in self.prs[1]["risk_factors"]])
        self.assertIsNone(self.prs[6]["reviewed"])

    def test_api_sizes_used(self) -> None:
        """PR sizes come from the API, not local diffs."""
        p = self.prs[1]
        self.assertEqual((p["additions"], p["deletions"], p["lines"], p["files"], p["commits"]), (999, 1, 1000, 7, 3))
        self.assertEqual(self.prs[10]["lines"], 350)

    def test_source_and_exact_fields(self) -> None:
        """API PRs are marked as API-sourced with nothing inferred."""
        for p in self.prs.values():
            self.assertEqual(p["source"], "api")
            self.assertEqual(p["inferred"], [])

    def test_local_enrichment(self) -> None:
        """API PRs are enriched from their local merge commits."""
        self.assertTrue(self.prs[1]["sensitive"])  # src/auth/login.py touched by the merge commit
        self.assertEqual(self.prs[1]["release"], "v1.0.0")
        self.assertIsNone(self.prs[10]["sensitive"])  # no local merge commit for #10

    def test_restore_resolved_via_api_shas(self) -> None:
        """Restore time is resolved through the API's merge commit SHAs."""
        self.assertEqual(self.prs[4]["restore"], self.prs[4]["merged"] - self.prs[3]["merged"])

    def test_since_stops_early(self) -> None:
        """--since stops paging once PRs are older than the cutoff."""
        day = time.strftime("%Y-%m-%d", time.gmtime(BASE + 4 * DAY))
        with ms.MockServer(ms.github_handler(desc_nodes(self.nodes), page_size=3)) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, "--since", day, env={"GITHUB_TOKEN": FAKE})
            n_requests = len(srv.requests)
        self.assertLess(n_requests, 4, "pagination must stop once PRs older than --since are seen")
        self.assertTrue(all(p["number"] >= 4 for p in d["prs"]))

    def test_max_prs(self) -> None:
        """--max-prs stops paging once enough PRs are fetched."""
        with ms.MockServer(ms.github_handler(desc_nodes(self.nodes), page_size=3)) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, "--max-prs", "4", env={"GITHUB_TOKEN": FAKE})
            self.assertEqual(len(srv.requests), 2)
        self.assertEqual(len(d["prs"]), 4)

    def test_auth_header_and_no_token_in_url(self) -> None:
        """The token is sent only as a bearer header, never in URLs or output."""
        for q in self.srv.requests:
            self.assertEqual(q["headers"]["authorization"], f"Bearer {FAKE}")
            self.assertNotIn(FAKE, q["raw_path"])
            self.assertEqual(q["path"], "/graphql")
        self.assertNotIn(FAKE, json.dumps(self.data))

    def test_graphql_errors_fall_back(self) -> None:
        """GraphQL errors fall back to git mode with a warning."""
        with ms.MockServer(lambda req: (200, {}, {"errors": [{"message": "Could not resolve to a Repository"}]})) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(any("Could not resolve" in w for w in d["meta"]["warnings"]))

    def test_repository_null_falls_back(self) -> None:
        """A null repository in the response falls back to git mode."""
        with ms.MockServer(lambda req: (200, {}, {"data": {"repository": None}})) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")

    def test_not_logged_in_falls_back_with_warning(self) -> None:
        """When gh has no login, auto mode uses git and says how to log in."""
        n = len(self.srv.requests)
        d, _ = collect(self.r, "--fake-api", self.srv.url)
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertEqual(len(self.srv.requests), n)
        self.assertTrue(any("not logged in" in w and "gh auth login --hostname github.com" in w
                            for w in d["meta"]["warnings"]), d["meta"]["warnings"])

    def test_source_git_never_calls_api(self) -> None:
        """--source git never contacts the API."""
        n = len(self.srv.requests)
        d, _ = collect(self.r, "--fake-api", self.srv.url, "--source", "git", env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(len(self.srv.requests), n)
        self.assertEqual(d["meta"]["mode"], "git")


def gitlab_fixture(r: Repo) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]],
                              dict[int, list[dict[str, Any]]]]:
    """Build mock GitLab MRs #1-#5 plus commits and notes; merged MRs point at `r`'s commits.

    Returns:
        (merge requests newest first, commits by iid, notes by iid).
    """
    sh = merge_shas(r)
    c = lambda n: BASE + n * DAY  # noqa: E731
    h = 3600
    mrs = [
        ms.gl_mr(5, "feat: locked", c(5), state="locked"),
        ms.gl_mr(4, "feat: open one", c(4)),
        ms.gl_mr(3, "chore: closed", c(3), closed=c(3) + h, state="closed"),
        ms.gl_mr(2, "fix: correct typo", c(2), merged=c(2) + 6 * h, merge_sha=None, squash_sha=sh[2], source="fix/typo"),
        ms.gl_mr(1, "feat(auth): add login", c(1), merged=c(1) + 9 * h, merge_sha=sh[1], source="feat/login"),
    ]
    commits = {1: [ms.gl_commit(c(1) - 2 * h), ms.gl_commit(c(1) - h)], 2: [ms.gl_commit(c(2) - h)]}
    notes = {1: [ms.gl_note("root", c(1) + 10, system=True), ms.gl_note("alice", c(1) + 20),       # system, author
                 ms.gl_note("ci-bot", c(1) + 30, bot=True), ms.gl_note("bob", c(1) + 2 * h)],
             2: [ms.gl_note("alice", c(2) + 5)]}
    return mrs, commits, notes


class B2_GitLab(unittest.TestCase):
    """GitLab REST client against a mock server."""

    @classmethod
    def setUpClass(cls) -> None:
        """Start a mock GitLab with five MRs and collect the repo through it once."""
        cls.r = api_repo("git@gitlab.com:grp/sub/proj.git")
        cls.mrs, cls.commits, cls.notes = gitlab_fixture(cls.r)
        cls.srv = ms.MockServer(ms.gitlab_handler(cls.mrs, cls.commits, cls.notes, page_size=2)).start()
        cls.data, _ = collect(cls.r, "--fake-api", cls.srv.url, env={"GITLAB_TOKEN": FAKE})
        cls.prs = by_number(cls.data)

    @classmethod
    def tearDownClass(cls) -> None:
        """Stop the mock server."""
        cls.srv.stop()

    def test_provider_and_pagination(self) -> None:
        """GitLab is detected and MR lists are paged for the URL-encoded project."""
        self.assertEqual(self.data["meta"]["mode"], "api")
        self.assertEqual(self.data["meta"]["provider"], "gitlab")
        lists = [q for q in self.srv.requests if q["path"].endswith("/merge_requests")]
        self.assertEqual([q["query"]["page"][0] for q in lists], ["1", "2", "3"])
        self.assertTrue(all(q["query"]["target_branch"] == ["main"] for q in lists))
        self.assertTrue(all("%2F" in q["raw_path"] and "grp%2Fsub%2Fproj" in q["raw_path"] for q in lists))
        self.assertEqual(sorted(self.prs), [1, 2, 3, 4, 5])

    def test_header_not_url(self) -> None:
        """The token goes in the PRIVATE-TOKEN header, never the URL."""
        for q in self.srv.requests:
            self.assertEqual(q["headers"]["private-token"], FAKE)
            self.assertNotIn(FAKE, q["raw_path"])
            self.assertNotIn("authorization", q["headers"])

    def test_state_mapping(self) -> None:
        """GitLab MR states map to merged, closed and open."""
        self.assertEqual({n: self.prs[n]["state"] for n in self.prs}, {1: "merged", 2: "merged", 3: "closed", 4: "open", 5: "open"})

    def test_reviews_ignore_system_author_bot(self) -> None:
        """First review ignores system notes, the author and bots."""
        c1, h = BASE + DAY, 3600
        p = self.prs[1]
        self.assertEqual(p["first_review"], c1 + 2 * h)
        self.assertTrue(p["reviewed"])
        self.assertFalse(self.prs[2]["reviewed"])  # only the author commented
        self.assertIn(["unreviewed", 10], self.prs[2]["risk_factors"])
        self.assertEqual(p["first_commit"], c1 - 2 * h)
        self.assertEqual(p["commits"], 2)

    def test_size_from_local_merge_and_squash_commit(self) -> None:
        """MR sizes come from the local merge or squash commit."""
        self.assertEqual((self.prs[1]["lines"], self.prs[1]["files"]), (110, 2))  # merge commit
        self.assertEqual((self.prs[2]["lines"], self.prs[2]["files"]), (1, 1))    # via squash_commit_sha
        self.assertIsNone(self.prs[4]["lines"])                                    # no local commit

    def test_inferred_only_ready(self) -> None:
        """Only the ready time is inferred for GitLab MRs."""
        self.assertEqual(self.prs[1]["inferred"], ["ready"])

    def test_no_reviews_flag(self) -> None:
        """--no-reviews skips the notes requests and review metrics."""
        with ms.MockServer(ms.gitlab_handler(self.mrs, self.commits, self.notes, page_size=2)) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, "--no-reviews", env={"GITLAB_TOKEN": FAKE})
            self.assertFalse([q for q in srv.requests if q["path"].endswith("/notes")])
        p = by_number(d)[1]
        self.assertIsNone(p["first_review"])
        self.assertIsNone(p["reviewed"])
        self.assertIn("first_review", p["inferred"])
        self.assertFalse(d["meta"]["reviews"])
        self.assertNotIn("unreviewed", [f[0] for f in p["risk_factors"]])

    def test_since_sends_updated_after(self) -> None:
        """--since is sent as updated_after, not created_after."""
        # updated_after (not created_after): an MR opened before --since but merged after it must still be fetched.
        with ms.MockServer(ms.gitlab_handler(self.mrs, self.commits, self.notes, page_size=50)) as srv:
            collect(self.r, "--fake-api", srv.url, "--since", "2022-02-05", env={"GITLAB_TOKEN": FAKE})
            q = [q for q in srv.requests if q["path"].endswith("/merge_requests")][0]
        self.assertEqual(q["query"]["updated_after"], ["2022-02-05T00:00:00Z"])
        self.assertNotIn("created_after", q["query"])

    def test_all_bases_drops_target_filter(self) -> None:
        """--all-bases drops the target_branch filter."""
        with ms.MockServer(ms.gitlab_handler(self.mrs, self.commits, self.notes, page_size=50)) as srv:
            collect(self.r, "--fake-api", srv.url, "--all-bases", env={"GITLAB_TOKEN": FAKE})
            q = [q for q in srv.requests if q["path"].endswith("/merge_requests")][0]
        self.assertNotIn("target_branch", q["query"])

    def test_detail_error_does_not_crash(self) -> None:
        """A failing per-MR commits request does not abort API mode."""
        def h(req: ms.Request) -> ms.Response:
            """Fail every commits request with 500; answer the rest as the GitLab mock."""
            if req["path"].endswith("/commits"):
                return 500, {}, {"message": "boom"}
            return ms.gitlab_handler(self.mrs, self.commits, self.notes, page_size=50)(req)

        with mock.patch.object(rs, "_backoff", lambda s: None), ms.MockServer(h) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "api")


class B3_CliLogin(unittest.TestCase):
    """repo-stats never handles a credential: gh and glab use their own login, and nothing leaks."""

    def _run(self, remote: str, provider_env: str, handler: ms.Handler,
             extra: Sequence[str] = ()) -> tuple[subprocess.CompletedProcess[str], Path, list[ms.Request]]:
        """Run the CLI with all three outputs against a mock API, with a fake token for the fake CLI.

        Args:
            remote: Origin URL for the test repo.
            provider_env: Environment variable the fake gh/glab reads its token from.
            handler: Mock API handler.
            extra: Extra CLI arguments.

        Returns:
            (completed process, output directory, requests the mock received).
        """
        r = api_repo(remote)
        out = tmpdir()
        with ms.MockServer(handler) as srv:
            p = run_cli([r.path, "-o", out / "o.html", "--json", out / "o.json", "--csv", out / "o.csv",
                         "--fake-api", srv.url, *extra], env={provider_env: FAKE})
            reqs = list(srv.requests)
        return p, out, reqs

    def _assert_no_leak(self, p: subprocess.CompletedProcess[str], out: Path) -> None:
        """Assert the run succeeded and the token is in no output file or stream."""
        self.assertEqual(p.returncode, 0, p.stderr)
        for f in ("o.html", "o.json", "o.csv"):
            self.assertNotIn(FAKE, (out / f).read_text(), f)
        self.assertNotIn(FAKE, p.stderr)
        self.assertNotIn(FAKE, p.stdout)

    def test_github_cli_no_leak(self) -> None:
        """A run through gh (logged in via GITHUB_TOKEN) gets API data and leaks the token nowhere."""
        nodes = github_nodes(fixtures.merge_workflow_repo())
        p, out, reqs = self._run("https://github.com/acme/widgets.git", "GITHUB_TOKEN",
                                 ms.github_handler(desc_nodes(nodes), page_size=50, expect_token=FAKE))
        self._assert_no_leak(p, out)
        self.assertIn("source: api", p.stderr)
        self.assertIn("with `gh api`", p.stderr)
        self.assertTrue(reqs)

    def test_gitlab_cli_no_leak(self) -> None:
        """A run through glab (logged in via GITLAB_TOKEN) gets API data and leaks the token nowhere."""
        mrs, commits, notes = gitlab_fixture(fixtures.merge_workflow_repo())
        p, out, reqs = self._run("https://gitlab.com/grp/proj.git", "GITLAB_TOKEN", ms.gitlab_handler(mrs, commits, notes, page_size=50))
        self._assert_no_leak(p, out)
        self.assertIn("source: api", p.stderr)
        for q in reqs:
            self.assertEqual(q["headers"]["private-token"], FAKE)

    def test_credentials_embedded_in_remote_not_leaked(self) -> None:
        """Credentials in the remote URL are never sent or written."""
        nodes = github_nodes(fixtures.merge_workflow_repo())
        r = api_repo("https://octo:EMBEDDEDSECRET99@github.com/acme/widgets.git")
        out = tmpdir()
        log = tmpdir() / "cli.log"
        with ms.MockServer(ms.github_handler(desc_nodes(nodes), page_size=50)) as srv:
            p = run_cli([r.path, "-o", out / "o.html", "--json", out / "o.json", "--fake-api", srv.url],
                        env={"GITHUB_TOKEN": FAKE, "REPO_STATS_FAKE_LOG": str(log)})
            for q in srv.requests:
                self.assertNotIn("EMBEDDEDSECRET99", json.dumps(q["headers"]) + q["raw_path"])
        self.assertEqual(p.returncode, 0, p.stderr)
        for f in ("o.html", "o.json"):
            self.assertNotIn("EMBEDDEDSECRET99", (out / f).read_text())
        self.assertNotIn("EMBEDDEDSECRET99", p.stderr)
        self.assertNotIn("EMBEDDEDSECRET99", json.dumps([c["argv"] for c in helpers.cli_calls(log)]))

    def test_token_on_api_failure_not_in_stderr(self) -> None:
        """A failed API run does not print the token."""
        r = api_repo()
        with ms.MockServer(lambda req: (401, {}, {"message": "Bad credentials"})) as srv:
            p = run_cli([r.path, "-o", tmpdir() / "o.html", "--fake-api", srv.url, "--source", "api"], env={"GITHUB_TOKEN": FAKE})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("gh auth login", p.stderr)
        self.assertNotIn(FAKE, p.stderr + p.stdout)

    def test_not_logged_in_means_no_request(self) -> None:
        """When gh has no login, no API request is made and the run falls back to git."""
        r = api_repo()
        with ms.MockServer(lambda req: (200, {}, {})) as srv:
            p = run_cli([r.path, "-o", tmpdir() / "o.html", "--fake-api", srv.url])
            self.assertEqual(srv.requests, [])
        self.assertEqual(p.returncode, 0)
        self.assertIn("not logged in", p.stderr)

    def test_source_api_not_logged_in_is_error(self) -> None:
        """--source api without a gh login exits 1 with the login command, no traceback."""
        r = api_repo()
        p = run_cli([r.path, "-o", tmpdir() / "o.html", "--source", "api"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("gh auth login --hostname github.com", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_login_hint_names_the_variable_gh_reads_for_the_host(self) -> None:
        """github.com needs GH_TOKEN; GitHub Enterprise Server needs GH_ENTERPRISE_TOKEN, and GH_TOKEN is not enough."""
        self.assertEqual(rs.ci_login_hint("gh", "github.com"), "GH_TOKEN")
        self.assertEqual(rs.ci_login_hint("gh", "acme.ghe.com"), "GH_TOKEN")
        self.assertEqual(rs.ci_login_hint("gh", "ghe.corp.example"), "GH_ENTERPRISE_TOKEN")
        self.assertIn("GITLAB_TOKEN", rs.ci_login_hint("glab", "gitlab.com"))
        r = api_repo("https://ghe.corp.example/acme/widgets.git")
        d, _ = collect(r, "--provider", "github", "--hostname", "ghe.corp.example", env={"GH_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(any("not logged in to ghe.corp.example" in w and "set GH_ENTERPRISE_TOKEN" in w
                            for w in d["meta"]["warnings"]), d["meta"]["warnings"])

    def test_glab_without_login_falls_back_with_the_login_command(self) -> None:
        """glab with no login sends an anonymous request; the 401 falls back to git and names `glab auth login`."""
        r = api_repo("git@gitlab.com:grp/proj.git")
        mrs, commits, notes = gitlab_fixture(r)
        with ms.MockServer(ms.gitlab_handler(mrs, commits, notes, page_size=50)) as srv:
            d, _ = collect(r, "--fake-api", srv.url)
            self.assertTrue(srv.requests)
            self.assertTrue(all("private-token" not in q["headers"] for q in srv.requests))
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(any("401" in w and "glab auth login --hostname gitlab.com" in w for w in d["meta"]["warnings"]),
                        d["meta"]["warnings"])
        with ms.MockServer(ms.gitlab_handler(mrs, commits, notes, page_size=50)) as srv:
            with self.assertRaises(rs.ApiError) as cm:
                collect(r, "--fake-api", srv.url, "--source", "api")
        self.assertIn("glab auth login", str(cm.exception))

    def test_missing_cli_falls_back_or_errors(self) -> None:
        """Without gh installed, auto mode uses git and says where to get it; --source api is an error."""
        r = api_repo()
        empty = tmpdir()  # a PATH with no gh on it (git still runs from the real PATH)
        d, _ = collect(r, env={"PATH": str(empty), "GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(any("gh is not installed" in w and "cli.github.com" in w for w in d["meta"]["warnings"]))
        with self.assertRaises(rs.CliUnavailable):
            collect(r, "--source", "api", env={"PATH": str(empty)})

    def test_missing_cli_warns_on_the_command_line(self) -> None:
        """Without gh on PATH, the run prints a warning, uses local git history and says so."""
        r = api_repo()
        empty = tmpdir()
        # Only git on PATH: the directory git lives in may also hold a real gh, which must never run here.
        git = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
        os.symlink(git, empty / "git")
        p = run_cli([r.path, "-o", tmpdir() / "o.html"], env={"PATH": str(empty)})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("warning: gh is not installed", p.stderr)
        self.assertIn("PRs are inferred from git history", p.stderr)
        self.assertIn("source: git", p.stderr)

    def test_collect_passes_given_environ_to_the_cli(self) -> None:
        """collect() runs gh with its environ argument, not os.environ."""
        nodes = github_nodes(fixtures.merge_workflow_repo())
        r = api_repo()
        with ms.MockServer(ms.github_handler(desc_nodes(nodes), page_size=50)) as srv:
            args = rs.build_parser().parse_args([str(r.path)])
            env = helpers.clean_env({"GITHUB_TOKEN": "from-param", "REPO_STATS_FAKE_URL": srv.url})
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "from-os-environ"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                d = rs.collect(args, environ=env)
            auths = [q["headers"].get("authorization") for q in srv.requests]
        self.assertEqual(d["meta"]["mode"], "api")
        self.assertEqual(set(auths), {"Bearer from-param"})

    def test_repo_stats_never_reads_a_credential(self) -> None:
        """The source never looks up a credential variable's value; it only removes the names for git."""
        src = (ROOT / "repo_stats.py").read_text()
        for name in rs.CREDENTIAL_VARS:
            for pat in (rf"environ(?:\.get)?\(\s*[\"']{name}", rf"environ\[\s*[\"']{name}", rf"getenv\(\s*[\"']{name}"):
                self.assertIsNone(re.search(pat, src), (name, pat))
        self.assertNotIn("Authorization", src)
        self.assertNotIn("PRIVATE-TOKEN", src)


class B4_Failures(unittest.TestCase):
    """API failure handling and fallback to git."""

    @classmethod
    def setUpClass(cls) -> None:
        """Build the repo with a GitHub remote shared by these tests."""
        cls.r = api_repo()

    def _srv(self, status: int, headers: Mapping[str, str] | None = None) -> ms.MockServer:
        """Return a mock server that answers every request with `status`."""
        return ms.MockServer(lambda req: (status, headers or {}, {"message": "nope"}))

    def test_auto_falls_back_for_4xx(self) -> None:
        """Auto mode falls back to git on 401, 403 and 404 with a warning."""
        for status, needle in ((401, "401"), (403, "403"), (404, "404")):
            with self._srv(status) as srv:
                d, _ = collect(self.r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
            self.assertEqual(d["meta"]["mode"], "git", status)
            self.assertEqual(len(d["prs"]), 5)
            self.assertTrue(any("API fetch failed" in w and needle in w for w in d["meta"]["warnings"]), (status, d["meta"]["warnings"]))

    def test_api_source_errors_for_4xx_cli(self) -> None:
        """--source api exits 1 with the status code on 4xx errors."""
        for status in (401, 403, 404):
            with self._srv(status) as srv:
                p = run_cli([self.r.path, "-o", tmpdir() / "o.html", "--fake-api", srv.url, "--source", "api"], env={"GITHUB_TOKEN": FAKE})
            self.assertEqual(p.returncode, 1, status)
            self.assertIn(f"{status}", p.stderr)
            self.assertIn("error:", p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    def test_500_retries_then_falls_back(self) -> None:
        """Server errors are retried with exponential back-off, then fall back."""
        sleeps = []
        with mock.patch.object(rs, "_backoff", sleeps.append), self._srv(500) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
            self.assertEqual(len(srv.requests), 5)
        self.assertEqual(sleeps, [2, 4, 8, 16])
        self.assertEqual(d["meta"]["mode"], "git")

    def test_500_with_source_api_raises(self) -> None:
        """--source api raises ToolError after repeated 500s."""
        with mock.patch.object(rs, "_backoff", lambda s: None), self._srv(500) as srv:
            with self.assertRaises(rs.ToolError) as cm:
                collect(self.r, "--fake-api", srv.url, "--source", "api", env={"GITHUB_TOKEN": FAKE})
        self.assertIn("500", str(cm.exception))

    def test_connection_refused(self) -> None:
        """A refused connection falls back quickly, or raises with --source api."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sleeps = []
        with mock.patch.object(rs, "_backoff", sleeps.append):
            d, _ = collect(self.r, "--fake-api", f"http://127.0.0.1:{port}", env={"GITHUB_TOKEN": FAKE})
            self.assertEqual(d["meta"]["mode"], "git")
            self.assertTrue(any("Could not reach" in w for w in d["meta"]["warnings"]))
            with self.assertRaises(rs.ToolError):
                collect(self.r, "--fake-api", f"http://127.0.0.1:{port}", "--source", "api", env={"GITHUB_TOKEN": FAKE})
        # UX: a dead host should not make the user wait this long before the git fallback kicks in.
        self.assertLessEqual(sum(sleeps) / 2, 10, f"waited {sum(sleeps) / 2}s of back-off before giving up on a refused connection")

    def test_429_retry_after_then_success(self) -> None:
        """A 429 waits Retry-After seconds, then succeeds."""
        nodes = desc_nodes(github_nodes(self.r))
        state = {"n": 0}
        inner = ms.github_handler(nodes, page_size=50)

        def h(req: ms.Request) -> ms.Response:
            """Answer the first request with 429 and Retry-After: 3, the rest normally."""
            state["n"] += 1
            if state["n"] == 1:
                return 429, {"Retry-After": "3"}, {"message": "slow down"}
            return inner(req)

        sleeps = []
        with mock.patch.object(rs, "_backoff", sleeps.append), ms.MockServer(h) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(sleeps, [3])
        self.assertEqual(d["meta"]["mode"], "api")

    def test_429_retry_after_http_date_does_not_crash(self) -> None:
        """A Retry-After given as an HTTP date does not crash."""
        nodes = desc_nodes(github_nodes(self.r))
        state = {"n": 0}
        inner = ms.github_handler(nodes, page_size=50)

        def h(req: ms.Request) -> ms.Response:
            """Answer the first request with 429 and an HTTP-date Retry-After, the rest normally."""
            state["n"] += 1
            if state["n"] == 1:
                return 429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, {"message": "slow down"}
            return inner(req)

        with mock.patch.object(rs, "_backoff", lambda s: None), ms.MockServer(h) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "api")

    def test_non_json_200_falls_back(self) -> None:
        """A non-JSON 200 response falls back to git cleanly.

        An HTML page (captive portal, proxy error) with status 200 must be a clean fallback, not a traceback.
        """
        with ms.MockServer(lambda req: (200, {"Content-Type": "text/html"}, "<html>proxy login</html>")) as srv:
            d, _ = collect(self.r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(any("API fetch failed" in w for w in d["meta"]["warnings"]))

    def test_non_json_200_cli_clean_error(self) -> None:
        """A non-JSON 200 with --source api exits 1 without a traceback."""
        with ms.MockServer(lambda req: (200, {}, "<html>nope</html>")) as srv:
            p = run_cli([self.r.path, "-o", tmpdir() / "o.html", "--fake-api", srv.url, "--source", "api"], env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_no_remote_auto_and_api(self) -> None:
        """Without a hosting remote, auto mode uses git and --source api errors."""
        r = fixtures.merge_workflow_repo()
        d, _ = collect(r, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")
        p = run_cli([r.path, "-o", tmpdir() / "o.html", "--source", "api"], env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(p.returncode, 1)
        self.assertIn("No GitHub or GitLab remote", p.stderr)


class B5_Safety(unittest.TestCase):
    """HTTP safety rules and remote URL parsing."""

    def test_parse_remote_styles(self) -> None:
        """parse_remote() handles HTTPS, SSH, scp-style and credentialed URLs."""
        cases = {
            "https://github.com/acme/widgets.git": ("github.com", "acme/widgets"),
            "https://github.com/acme/widgets": ("github.com", "acme/widgets"),
            "https://github.com/acme/widgets/": ("github.com", "acme/widgets"),
            "git@github.com:acme/widgets.git": ("github.com", "acme/widgets"),
            "ssh://git@gitlab.example.com:2222/grp/sub/proj.git": ("gitlab.example.com", "grp/sub/proj"),
            "ssh://git@github.com/acme/widgets.git": ("github.com", "acme/widgets"),
            "https://user:tok@git.corp.example/acme/widgets.git": ("git.corp.example", "acme/widgets"),
            "https://GitHub.com/Acme/Widgets.git": ("github.com", "Acme/Widgets"),
            "git.corp.example:team/app.git": ("git.corp.example", "team/app"),
            "https://gitlab.example.com:8443/a/b.git": ("gitlab.example.com", "a/b"),
        }
        for url, want in cases.items():
            self.assertEqual(rs.parse_remote(url), want, url)
        self.assertIsNone(rs.parse_remote("/some/local/path"))
        self.assertIsNone(rs.parse_remote(""))

    def test_detect_provider(self) -> None:
        """detect_provider() recognises github.com and gitlab.com only."""
        self.assertEqual(rs.detect_provider("github.com"), "github")
        self.assertEqual(rs.detect_provider("gitlab.com"), "gitlab")
        self.assertIsNone(rs.detect_provider("git.corp.example"))
        self.assertIsNone(rs.detect_provider("bitbucket.org"))

    def test_token_not_sent_to_lookalike_hosts(self) -> None:
        """Lookalike hosts are not auto-detected as GitHub or GitLab.

        Only github.com and gitlab.com may be auto-detected; any other host (github.evil.com, *gitlab*)
        needs an explicit --provider/--hostname, because gh and glab send environment tokens to any host.
        """
        for host in ("github.evil.com", "github.evil.example", "gitlab.evil.com", "notgitlab.example", "my-gitlab.corp.example",
                     "github.com.evil.example"):
            self.assertIsNone(rs.detect_provider(host), host)
        self.assertEqual(rs.detect_provider("github.com"), "github")
        self.assertEqual(rs.detect_provider("gitlab.com"), "gitlab")

    def test_lookalike_remote_gets_no_request(self) -> None:
        """A lookalike remote host gets no API request."""
        r = fixtures.merge_workflow_repo()
        add_remote(r, "https://github.evil.com/acme/widgets.git")
        d, _ = collect(r, env={"GITHUB_TOKEN": FAKE})  # must not even try https://github.evil.com/api
        self.assertEqual(d["meta"]["mode"], "git")

    def test_provider_and_hostname_override(self) -> None:
        """--provider and --hostname enable a self-hosted GitLab, and glab gets exactly that host."""
        r = fixtures.merge_workflow_repo()
        add_remote(r, "git@git.corp.example:team/app.git")
        d, _ = collect(r, env={"GITLAB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(any("No GitHub or GitLab remote" in w for w in d["meta"]["warnings"]))
        mrs, commits, notes = gitlab_fixture(r)
        log = tmpdir() / "cli.log"
        with ms.MockServer(ms.gitlab_handler(mrs, commits, notes, page_size=50)) as srv:
            d, _ = collect(r, "--provider", "gitlab", "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE})
            self.assertEqual(d["meta"]["mode"], "git", "--provider alone must not trust the remote's host")
            d, _ = collect(r, "--provider", "gitlab", "--hostname", "Git.Corp.Example", "--fake-api", srv.url,
                           env={"GITLAB_TOKEN": FAKE, "REPO_STATS_FAKE_LOG": str(log)})
            self.assertTrue(any("team%2Fapp" in q["raw_path"] for q in srv.requests))
        self.assertEqual(d["meta"]["mode"], "api")
        hosts = {c["argv"][c["argv"].index("--hostname") + 1] for c in helpers.cli_calls(log)}
        self.assertEqual(hosts, {"git.corp.example"})
        self.assertEqual(d["meta"]["name"], "app")

    def test_remote_option(self) -> None:
        """--remote picks the remote that names the API repository."""
        r = fixtures.merge_workflow_repo()
        add_remote(r, "https://github.com/acme/widgets.git", "upstream")
        nodes = desc_nodes(github_nodes(r))
        with ms.MockServer(ms.github_handler(nodes, page_size=50)) as srv:
            d, _ = collect(r, "--remote", "upstream", "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
            body = json.loads(srv.requests[0]["body"])
        self.assertEqual(body["variables"]["owner"], "acme")
        self.assertEqual(body["variables"]["name"], "widgets")
        self.assertEqual(d["meta"]["mode"], "api")


class B6_ReviewerFindings(unittest.TestCase):
    """Regression tests for issues found by an independent review."""

    def test_csv_empty_string_and_control_char_prefixes(self) -> None:
        """CSV keeps empty strings empty and neutralises control-character formula prefixes."""
        out = tmpdir() / "x.csv"
        rec = {"id": 1, "title": "", "type": "\r=1+1", "scope": None, "state": "\t=2", "source": "=3", "risk_level": "@x"}
        rs.write_csv(str(out), [rec])
        row = list(csv.DictReader(io.StringIO(Path(out).read_text(encoding="utf-8"))))[0]
        self.assertEqual(row["title"], "", "empty string must stay empty (no stray apostrophe)")
        for k in ("type", "state", "source", "risk_level"):
            self.assertFalse(row[k][:1] in ("=", "+", "-", "@", "\t", "\r"), (k, row[k]))

    def test_no_titles_leaves_no_scope(self) -> None:
        """--no-titles also hides conventional-commit scopes."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(2)})
        r.squash_pr(1, "feat(secretscopeword): launch", {"b.py": lines(3)})
        out = tmpdir()
        p = run_cli([r.path, "-o", out / "o.html", "--json", out / "o.json", "--csv", out / "o.csv", "--no-titles"])
        self.assertEqual(p.returncode, 0, p.stderr)
        for f in ("o.html", "o.json", "o.csv"):
            self.assertNotIn("secretscopeword", (out / f).read_text(), f)

    def test_auto_mode_falls_back_on_any_exception(self) -> None:
        """Auto mode falls back to git on any client exception."""
        r = api_repo()
        for exc in (ValueError("bad json"), TimeoutError("read timed out"), KeyError("data"), OSError("reset")):
            with mock.patch.object(rs.Cli, "_run", side_effect=exc):
                d, _ = collect(r, "--fake-api", "http://127.0.0.1:1", env={"GITHUB_TOKEN": FAKE})
            self.assertEqual(d["meta"]["mode"], "git", repr(exc))
            self.assertTrue(any("API fetch failed" in w for w in d["meta"]["warnings"]))

    def test_since_keeps_prs_opened_before_but_merged_after_cutoff(self) -> None:
        """--since keeps PRs opened before the cutoff but merged after it."""
        r = api_repo()
        sh = merge_shas(r)
        old = ms.gh_node(20, "feat: long running", BASE + DAY, merged=BASE + 12 * DAY, merge_oid=sh[1])
        new = ms.gh_node(21, "fix: newer", BASE + 8 * DAY, merged=BASE + 9 * DAY)
        older = ms.gh_node(19, "fix: really old", BASE - 20 * DAY, merged=BASE - 19 * DAY)
        since_day = time.strftime("%Y-%m-%d", time.gmtime(BASE + 5 * DAY))
        nodes = [new, old, older]  # created DESC
        with ms.MockServer(ms.github_handler(nodes, page_size=1)) as srv:
            d, _ = collect(r, "--fake-api", srv.url, "--since", since_day, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(sorted(p["number"] for p in d["prs"]), [20, 21])

    def test_branch_with_non_origin_remote_prefix_still_matches_api_base(self) -> None:
        """A branch on a non-origin remote still matches the API base branch."""
        r = api_repo()
        r.git("update-ref", "refs/remotes/upstream/main", "HEAD")
        nodes = desc_nodes(github_nodes(r))
        with ms.MockServer(ms.github_handler(nodes, page_size=50)) as srv:
            d, _ = collect(r, "--branch", "upstream/main", "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "api")
        self.assertGreater(len(d["prs"]), 0, "base-branch filter compared 'upstream/main' with 'main' and dropped every PR")

    def test_remote_option_with_default_branch_of_that_remote(self) -> None:
        """--remote uses that remote's default branch."""
        r = fixtures.merge_workflow_repo()
        add_remote(r, "https://github.com/acme/widgets.git", "upstream")
        r.git("update-ref", "refs/remotes/upstream/main", "HEAD")
        r.git("symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/main")
        nodes = desc_nodes(github_nodes(r))
        with ms.MockServer(ms.github_handler(nodes, page_size=50)) as srv:
            d, _ = collect(r, "--remote", "upstream", "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "api")
        self.assertGreater(len(d["prs"]), 0, "zero PRs with --remote upstream and no warning")

    def test_zero_api_prs_after_base_filter_warns(self) -> None:
        """Dropping every API PR by base branch produces a warning."""
        r = api_repo()
        nodes = [ms.gh_node(1, "feat: x", BASE, merged=BASE + 100, base="some-other-branch")]
        with ms.MockServer(ms.github_handler(nodes, page_size=50)) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        if not d["prs"]:
            self.assertTrue(d["meta"]["warnings"], "API returned PRs but all were filtered out by base branch, silently")


# =====================================================================================================
# C. Output safety
# =====================================================================================================

EVIL_TITLE = 'feat: </script><img src=x onerror=alert(1)> "q" \'s\' &amp; <!-- 😀'
EVIL_BRANCH = "feat/<img-src=x-onerror=alert(1)>"


class C1_HtmlSafety(unittest.TestCase):
    """Hostile titles and names cannot break out of the dashboard HTML."""

    @classmethod
    def setUpClass(cls) -> None:
        """Render a repo with script-breaking, formula and line-separator titles once."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(5)})
        r.merge_pr(1, EVIL_BRANCH, EVIL_TITLE, [("wip </script>", {"b.py": lines(3)})])
        r.squash_pr(2, "=cmd|' /C calc'!A0 </script>", {"c.py": lines(3)})
        r.squash_pr(3, "fix: line\u2028sep \u2029 para \x85 nel", {"d.py": lines(3)})
        cls.out = tmpdir()
        cls.p = run_cli([r.path, "-o", cls.out / "o.html", "--json", cls.out / "o.json", "--csv", cls.out / "o.csv",
                         "--title", "</script><b>t</b> & \"x\""])
        cls.html = (cls.out / "o.html").read_text(encoding="utf-8")

    def test_ran(self) -> None:
        """The CLI run with hostile input succeeds."""
        self.assertEqual(self.p.returncode, 0, self.p.stderr)

    def test_single_script_close_tag(self) -> None:
        """No extra </script> or <img> tags appear in the embedded data."""
        template = (ROOT / "template.html").read_text()
        self.assertEqual(self.html.count("</script>"), template.count("</script>"))
        self.assertNotIn("<img", self.html.split("const DATA = ", 1)[1].split("/* ---", 1)[0])

    def test_data_roundtrips(self) -> None:
        """Hostile strings round-trip unchanged through the embedded data."""
        d = extract_data(self.html)
        self.assertEqual(by_number(d)[1]["title"], EVIL_TITLE)
        self.assertEqual(d["meta"]["name"], "</script><b>t</b> & \"x\"")
        self.assertEqual(by_number(d)[2]["title"], "=cmd|' /C calc'!A0 </script>")
        self.assertEqual(by_number(d)[3]["title"], "fix: line\u2028sep \u2029 para \x85 nel")

    def test_no_raw_line_separators_in_data(self) -> None:
        """U+2028 and U+2029 are escaped in the embedded data."""
        blob = self.html.split("const DATA = ", 1)[1].split(";\n", 1)[0]
        self.assertNotIn("\u2028", blob)
        self.assertNotIn("\u2029", blob)

    def test_html_parses_to_expected_script_count(self) -> None:
        """An HTML parser finds one script element and no img elements."""
        from html.parser import HTMLParser

        class P(HTMLParser):
            """Count script and img start tags."""

            def __init__(self) -> None:
                """Start both counts at zero."""
                super().__init__()
                self.scripts, self.imgs = 0, 0

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                """Count `tag` if it is script or img."""
                self.scripts += tag == "script"
                self.imgs += tag == "img"

        p = P()
        p.feed(self.html)
        self.assertEqual((p.scripts, p.imgs), (1, 0))

    def test_template_uses_safe_dom_sinks(self) -> None:
        """The template uses no HTML-parsing DOM sinks or eval."""
        t = (ROOT / "template.html").read_text()
        self.assertNotIn("innerHTML", t)
        self.assertNotIn("outerHTML", t)
        self.assertNotIn("insertAdjacentHTML", t)
        self.assertNotIn("document.write", t)
        self.assertNotIn("eval(", t)


class C1b_UnicodeSeparatorsInMergeTitle(unittest.TestCase):
    """Unicode line separators in merge commit titles."""

    def test_merge_body_title_with_unicode_line_separator_not_truncated(self) -> None:
        """A merge title containing U+2028 is kept whole."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(5)})
        r.merge_pr(1, "feat/x", "feat: before\u2028after", [("wip", {"b.py": lines(3)})])
        d, _ = collect(r)
        self.assertEqual(d["prs"][0]["title"], "feat: before\u2028after")


class C2_CsvInjection(unittest.TestCase):
    """Spreadsheet formula injection in CSV output."""

    def test_formula_cells_neutralised(self) -> None:
        """Cells starting with =, +, -, @ or a tab get an apostrophe prefix."""
        r = Repo()
        r.commit("chore: initial commit", {"a.py": lines(5)})
        titles = ["=HYPERLINK(\"http://x\",\"y\")", "+1+1", "-2+3", "@SUM(1,1)", "\t=1+1", "normal title"]
        for i, t in enumerate(titles, 1):
            r.squash_pr(i, t, {f"f{i}.py": lines(2)})
        out = tmpdir()
        p = run_cli([r.path, "-o", out / "o.html", "--csv", out / "o.csv"])
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = list(csv.DictReader(io.StringIO((out / "o.csv").read_text(encoding="utf-8"))))
        self.assertEqual(len(rows), len(titles))
        for row in rows:
            for k, v in row.items():
                if k in ("title", "id", "type", "state", "source", "risk_level", "kind"):
                    self.assertFalse(v[:1] in ("=", "+", "-", "@", "\t", "\r"), (k, v))
        by_t = {row["title"].lstrip("'"): row["title"] for row in rows}
        self.assertTrue(by_t["+1+1"].startswith("'"))
        self.assertEqual(by_t["normal title"], "normal title")

    def test_empty_cells_stay_empty(self) -> None:
        """Empty CSV cells are not turned into a lone apostrophe."""
        out = tmpdir()
        run_cli([merge_repo().path, "-o", out / "o.html", "--csv", out / "o.csv"])
        rows = list(csv.DictReader(io.StringIO((out / "o.csv").read_text(encoding="utf-8"))))
        self.assertTrue(rows)
        for row in rows:
            for k, v in row.items():
                self.assertNotEqual(v, "'", (k, row["id"]))
        self.assertEqual(rows[0]["pickup"], "")


class C3_JsonValidity(unittest.TestCase):
    """Strict JSON validity of the outputs."""

    def _strict_load(self, text: str) -> Any:
        """Parse JSON, rejecting NaN and Infinity."""
        def bad(c: str) -> NoReturn:
            """Reject a non-standard JSON constant."""
            raise ValueError(f"non-standard JSON constant {c}")

        return json.loads(text, parse_constant=bad)

    def test_json_output_strict(self) -> None:
        """JSON output and embedded data are strict JSON with the expected keys."""
        for repo in (merge_repo().path, squash_repo().path, helpers.cached_synthetic(150)):
            out = tmpdir()
            p = run_cli([repo, "-o", out / "o.html", "--json", out / "o.json"])
            self.assertEqual(p.returncode, 0, p.stderr)
            d = self._strict_load((out / "o.json").read_text())
            self.assertEqual(set(d), {"meta", "prs", "loc", "deploys"})
            self._strict_load(json.dumps(extract_data((out / "o.html").read_text())))

    def test_zero_pr_json(self) -> None:
        """A repo with no PRs writes strict JSON with an empty PR list."""
        r = Repo()
        r.commit("chore: init", {"a.py": lines(3)})
        out = tmpdir()
        p = run_cli([r.path, "-o", out / "o.html", "--json", out / "o.json"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self._strict_load((out / "o.json").read_text())["prs"], [])

    def test_json_has_no_floats_nan(self) -> None:
        """Collected data contains no NaN or infinite floats."""
        d = merge_data()

        def walk(x: Any) -> None:
            """Recursively assert that every float in `x` is finite."""
            if isinstance(x, float):
                self.assertTrue(x == x and x not in (float("inf"), float("-inf")))
            elif isinstance(x, dict):
                [walk(v) for v in x.values()]
            elif isinstance(x, list):
                [walk(v) for v in x]

        walk(d)


class C4_NoRepoSpecificStrings(unittest.TestCase):
    """No identifying strings in the tool or its output."""

    def test_no_owner_repo_or_identity_in_tool_files(self) -> None:
        """Tool files and output contain no owner, repo, user identity or local paths."""
        wd = ROOT.parent
        needles = set()
        try:
            url = git_out(wd, "remote", "get-url", "origin")
            parsed = rs.parse_remote(url)
            if parsed:
                needles.update(x.lower() for x in parsed[1].split("/") if len(x) > 2)
        except Exception:
            pass
        for key in ("user.email", "user.name"):
            try:
                v = git_out(wd, "config", key)
                if key == "user.email" and "@" in v:
                    needles.add(v.lower())
                    if not v.endswith("noreply@anthropic.com"):
                        needles.add(v.split("@")[0].lower())
            except Exception:
                pass
        needles -= {"scripts", "claude", "noreply", "anthropic", "user", ""}
        sources = [ROOT / "repo_stats.py", ROOT / "template.html", ROOT / "tests" / "fixtures.py"]
        for f in sources:
            text = f.read_text().lower()
            for n in needles:
                self.assertNotIn(n, text, f"{n!r} found in {f.name}")
        # and the generated dashboard for a neutral fixture
        out = tmpdir()
        run_cli([merge_repo().path, "-o", out / "o.html"])
        html = (out / "o.html").read_text().lower()
        for n in needles:
            self.assertNotIn(n, html)
        self.assertNotIn(str(Path.home()).lower() + "/", html)
        self.assertNotIn(str(merge_repo().path).lower(), html)


# =====================================================================================================
# E. CLI and packaging (Python side)
# =====================================================================================================

class E1_Cli(unittest.TestCase):
    """Command-line behaviour."""

    def test_help_lists_all_flags(self) -> None:
        """--help lists every option."""
        p = run_cli(["--help"])
        self.assertEqual(p.returncode, 0)
        for action in rs.build_parser()._actions:
            for opt in action.option_strings:
                self.assertIn(opt, p.stdout, opt)

    def test_version(self) -> None:
        """--version prints the version."""
        p = run_cli(["--version"])
        self.assertEqual(p.returncode, 0)
        self.assertIn(rs.__version__, p.stdout + p.stderr)

    def test_invalid_regexes(self) -> None:
        """Invalid regexes exit 1 with a clear error."""
        r = merge_repo().path
        for flag in ("--exclude", "--bot-pattern", "--failure-pattern", "--sensitive-pattern", "--tag-pattern"):
            p = run_cli([r, "-o", tmpdir() / "o.html", flag, "("])
            self.assertEqual(p.returncode, 1, flag)
            self.assertIn("error:", p.stderr, flag)
            self.assertIn("regular expression", p.stderr, flag)
            self.assertNotIn("Traceback", p.stderr, flag)

    def test_invalid_dates(self) -> None:
        """Invalid dates exit 1 with a clear error."""
        r = merge_repo().path
        for flag in ("--since", "--until"):
            for bad in ("2022-13-45", "yesterday", "2022/01/01", "2022-1-5", "0d", "1q", "22", "2022-02-30"):
                p = run_cli([r, "-o", tmpdir() / "o.html", flag, bad])
                self.assertEqual(p.returncode, 1, (flag, bad))
                self.assertIn("Invalid date", p.stderr)
                self.assertNotIn("Traceback", p.stderr)

    def test_bad_arguments_exit_2(self) -> None:
        """Bad argument values exit 2."""
        p = run_cli(["--source", "bogus"])
        self.assertEqual(p.returncode, 2)
        p = run_cli([merge_repo().path, "--max-prs", "abc"])
        self.assertEqual(p.returncode, 2)

    def test_default_output_file_in_cwd(self) -> None:
        """Without -o the dashboard is written to ./repo-stats.html."""
        d = tmpdir()
        p = run_cli([merge_repo().path], cwd=d)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((d / "repo-stats.html").exists())

    def test_stdout_clean_and_summary_on_stderr(self) -> None:
        """stdout stays empty and the summary goes to stderr."""
        p = run_cli([merge_repo().path, "-o", tmpdir() / "o.html"])
        self.assertEqual(p.stdout, "")
        self.assertRegex(p.stderr, r"Wrote .*o\.html \(5 PRs, 5 merged, source: git\)")


class E2_Serve(unittest.TestCase):
    """The --serve mode."""

    def _start(self, out_dir: Path, name: str = "o.html") -> tuple[subprocess.Popen[str], str]:
        """Start the CLI with --serve on a free port and wait for it to print its URL.

        Returns:
            (server process, dashboard URL). Fails the test if no URL appears within 20 s.
        """
        proc = subprocess.Popen([sys.executable, SCRIPT, str(merge_repo().path), "-o", str(out_dir / name), "--serve", "0", "--no-open"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=helpers.clean_env())
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                self.addCleanup(pipe.close)
        url = None
        deadline = time.time() + 20
        buf = []
        while time.time() < deadline:
            line = proc.stderr.readline()  # pyright: ignore[reportOptionalMemberAccess]
            if not line:
                break
            buf.append(line)
            m = re.search(r"Serving (http://127\.0\.0\.1:(\d+)/\S+)", line)
            if m:
                url = m.group(1)
                break
        if not url:
            proc.kill()
            self.fail("server did not start: " + "".join(buf))
        return proc, url

    def _get(self, url: str) -> tuple[int, str, Message]:
        """GET `url` and return (status, body, headers), for error statuses too."""
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    def test_serve_loopback_200_and_sigint(self) -> None:
        """The server listens on loopback only, serves the page and exits cleanly on SIGINT."""
        out = tmpdir()
        proc, url = self._start(out)
        try:
            status, body, headers = self._get(url)
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("const DATA", body)
            port = int(re.search(r":(\d+)/", url).group(1))  # pyright: ignore[reportOptionalMemberAccess]
            listening = []
            for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    for ln in Path(fn).read_text().splitlines()[1:]:
                        f = ln.split()
                        if f[3] == "0A" and int(f[1].split(":")[1], 16) == port:
                            listening.append(f[1].split(":")[0])
                except OSError:
                    pass
            if not Path("/proc/net/tcp").exists():
                self.skipTest("no /proc/net/tcp on this platform; loopback binding checked on Linux only")
            self.assertTrue(listening, "listening socket not found")
            for addr in listening:
                self.assertIn(addr, ("0100007F", "00000000000000000000000001000000"), f"bound to non-loopback {addr}")
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("did not stop on SIGINT")
        err = proc.stderr.read()  # pyright: ignore[reportOptionalMemberAccess]
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)

    def test_serves_only_the_dashboard(self) -> None:
        """Only the dashboard is served, not other files in its directory.

        The output directory may be a project directory (default -o is ./repo-stats.html). Serving it
        whole would publish every file in it, including a directory listing, to anything on localhost.
        """
        out = tmpdir()
        (out / "secret.txt").write_text("TOP-SECRET")
        proc, url = self._start(out)
        try:
            base = url.rsplit("/", 1)[0]
            status, body, _ = self._get(base + "/secret.txt")
            self.assertEqual(status, 404, "unrelated file in the output directory was served")
            self.assertNotIn("TOP-SECRET", body)
            status, body, _ = self._get(base + "/")
            self.assertNotIn("secret.txt", body, "directory listing exposes other files")
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)

    def _stop(self, proc: subprocess.Popen[str]) -> None:
        """Stop the server with SIGINT, killing it if it does not exit within 10 s."""
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    def _get_with_host(self, url: str, host: str, method: str = "GET") -> tuple[int, str]:
        """Send `method` for `url` to the server with an explicit Host header and return (status, body)."""
        parts = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=10)
        try:
            conn.putrequest(method, parts.path, skip_host=True)
            conn.putheader("Host", host)
            conn.endheaders()
            r = conn.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def test_host_header_loopback_names_allowed(self) -> None:
        """Host 127.0.0.1:<port> and localhost:<port> (any case) get the dashboard."""
        proc, url = self._start(tmpdir())
        try:
            port = urllib.parse.urlsplit(url).port
            for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LocalHost:{port}"):
                status, body = self._get_with_host(url, host)
                self.assertEqual(status, 200, host)
                self.assertIn("const DATA", body, host)
        finally:
            self._stop(proc)

    def test_host_header_foreign_rejected(self) -> None:
        """A foreign Host (DNS rebinding) or the wrong port gets 421 and no dashboard, for GET and HEAD."""
        proc, url = self._start(tmpdir())
        try:
            port = urllib.parse.urlsplit(url).port or 0
            wrong = port + 1 if port < 65535 else port - 1
            for host in (f"evil.example:{port}", f"127.0.0.1:{wrong}", f"localhost:{wrong}", "127.0.0.1", "localhost"):
                for method in ("GET", "HEAD"):
                    status, body = self._get_with_host(url, host, method)
                    self.assertEqual(status, 421, f"{method} {host}")
                    self.assertNotIn("const DATA", body, f"{method} {host}")
        finally:
            self._stop(proc)

    def test_host_header_missing_rejected(self) -> None:
        """A request with no Host header at all gets 421."""
        proc, url = self._start(tmpdir())
        try:
            parts = urllib.parse.urlsplit(url)
            conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=10)
            try:
                conn.putrequest("GET", parts.path, skip_host=True)
                conn.endheaders()
                r = conn.getresponse()
                self.assertEqual(r.status, 421)
                self.assertNotIn("const DATA", r.read().decode("utf-8", "replace"))
            finally:
                conn.close()
        finally:
            self._stop(proc)


class E3_Python38Syntax(unittest.TestCase):
    """repo_stats.py stays compatible with Python 3.8."""

    def test_py_compile(self) -> None:
        """repo_stats.py compiles, also with other installed Python versions."""
        import py_compile
        pyc = str(tmpdir() / "x.pyc")
        py_compile.compile(str(ROOT / "repo_stats.py"), doraise=True, cfile=pyc)
        for exe in ("python3.8", "python3.9", "python3.11", "python3.12"):
            if self._has(exe):
                p = subprocess.run([exe, "-c", "import sys,py_compile; py_compile.compile(sys.argv[1], doraise=True, cfile=sys.argv[2])",
                                    str(ROOT / "repo_stats.py"), pyc], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                # Compiling only checks syntax; running --version also evaluates every module-level import,
                # type alias and TypedDict under that interpreter.
                p = subprocess.run([exe, SCRIPT, "--version"], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, f"{exe}: {p.stderr}")

    @staticmethod
    def _has(exe: str) -> bool:
        """Return whether `exe` is on PATH."""
        from shutil import which
        return which(exe) is not None

    def test_parses_as_python_38(self) -> None:
        """repo_stats.py parses with the Python 3.8 grammar."""
        src = (ROOT / "repo_stats.py").read_text()
        ast.parse(src, feature_version=(3, 8))

    def test_future_annotations_present(self) -> None:
        """repo_stats.py starts with `from __future__ import annotations`."""
        src = (ROOT / "repo_stats.py").read_text()
        tree = ast.parse(src)
        body: list[Any] = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
        self.assertIsInstance(body[0], ast.ImportFrom)
        self.assertEqual(body[0].module, "__future__")
        self.assertEqual([a.name for a in body[0].names], ["annotations"])

    def test_no_py39plus_only_features(self) -> None:
        """repo_stats.py uses no 3.9+ APIs, runtime builtin generics or X | Y unions."""
        src = (ROOT / "repo_stats.py").read_text()
        banned = [r"\.removeprefix\(", r"\.removesuffix\(", r"zoneinfo", r"functools\.cache\b", r"\.bit_count\(", r"math\.lcm",
                  r"\.is_relative_to\(", r"\bmatch\s+\w+:\s*$", r"datetime\.fromisoformat", r"\bgraphlib\b", r"\bTaskGroup\b",
                  r"\btomllib\b"]
        for pat in banned:
            self.assertIsNone(re.search(pat, src, re.M), pat)
        tree = ast.parse(src)
        in_annotation = set()
        for node in ast.walk(tree):
            anns = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                anns = [x.annotation for x in a.args + a.kwonlyargs + a.posonlyargs + [a.vararg, a.kwarg] if x is not None and x.annotation]
                anns += [node.returns] if node.returns else []
            elif isinstance(node, ast.AnnAssign):
                anns = [node.annotation]
            for ann in anns:
                in_annotation.update(id(n) for n in ast.walk(ann))
        for node in ast.walk(tree):
            if id(node) in in_annotation:
                continue  # annotations are never evaluated thanks to `from __future__ import annotations`
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("list", "dict", "set", "tuple", "type"):
                self.fail(f"runtime builtin generic at line {node.lineno} (3.9+)")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                for side in (node.left, node.right):
                    if (isinstance(side, ast.Constant) and side.value is None) or (isinstance(side, ast.Name) and side.id in ("str", "int", "dict", "list")):
                        self.fail(f"runtime X | Y union at line {node.lineno} (3.10+)")
                if isinstance(node.left, ast.Dict):
                    self.fail(f"dict | dict at line {node.lineno} (3.9+)")


class E4_Imports(unittest.TestCase):
    """Dependencies of repo_stats.py."""

    def test_only_stdlib_imports(self) -> None:
        """repo_stats.py imports only the standard library."""
        names = getattr(sys, "stdlib_module_names", None)
        if names is None:
            self.skipTest("sys.stdlib_module_names needs Python 3.10+")
        for fname in ("repo_stats.py",):
            tree = ast.parse((ROOT / fname).read_text())
            mods = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    mods.update(a.name.split(".")[0] for a in n.names)
                elif isinstance(n, ast.ImportFrom) and n.level == 0:
                    mods.add((n.module or "").split(".")[0])
            self.assertTrue(mods)
            self.assertEqual(sorted(m for m in mods if m not in names), [])

    def test_runs_isolated_mode(self) -> None:
        """The script runs in isolated mode (python -I)."""
        p = subprocess.run([sys.executable, "-I", SCRIPT, "--version"], capture_output=True, text=True, env=helpers.clean_env())
        self.assertEqual(p.returncode, 0, p.stderr)


class E5_SelfContained(unittest.TestCase):
    """The dashboard loads nothing from the network."""

    RESOURCE = re.compile(r"""(?:<script[^>]+src|<link[^>]+href|<img[^>]+src|<iframe[^>]+src|<source[^>]+src|<video[^>]+src|<audio[^>]+src)\s*=\s*["']?\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//|@import|\bfetch\(|XMLHttpRequest|WebSocket\(|EventSource\(|navigator\.sendBeacon|importScripts""", re.I)

    def test_template_has_no_remote_resource_loads(self) -> None:
        """The template has no remote resource loads or network calls."""
        t = (ROOT / "template.html").read_text()
        self.assertIsNone(self.RESOURCE.search(t), self.RESOURCE.search(t) and self.RESOURCE.search(t).group(0))  # pyright: ignore[reportOptionalMemberAccess]
        for m in re.finditer(r"https?://[^\s\"'<>)]+", t):
            self.assertIn(m.group(0), ("http://www.w3.org/2000/svg",), m.group(0))

    def test_generated_html_has_no_remote_resource_loads(self) -> None:
        """Generated HTML has no remote resource loads or network calls."""
        out = tmpdir()
        run_cli([merge_repo().path, "-o", out / "o.html"])
        html = (out / "o.html").read_text()
        self.assertIsNone(self.RESOURCE.search(html))



# =====================================================================================================
# F. Regression tests for review findings
# =====================================================================================================

class F1_CommitMessageInjection(unittest.TestCase):
    """Control characters in commit messages cannot forge diff stats, hide commits or crash the scan."""

    def _repo(self, body: str) -> Repo:
        """A repo whose second commit changes one line and carries `body` in its message."""
        r = Repo()
        r.commit("chore: init", {"a.py": "a\n"})
        r.commit("feat: innocent (#3)\n\nline one\n" + body, {"a.py": "a\nb\n"})
        return r

    def test_forged_numstat_in_body_is_ignored(self) -> None:
        """Fake numstat lines after a \\x1f in the body do not change size, sensitivity or lines of code."""
        r = self._repo("x\x1f\n999999\t0\tsrc/fake.py\n5\t0\tdb/migrate/x.sql\n")
        d, _ = collect(r)
        p = by_number(d)[3]
        self.assertEqual((p["lines"], p["files"], p["sensitive"]), (1, 1, False))
        self.assertEqual(d["loc"]["days"][-1][1], 2)
        self.assertEqual(list(d["loc"]["langs"]), ["Python"])

    def test_record_separator_in_body_does_not_crash_or_hide(self) -> None:
        """A \\x1e line with bogus fields neither crashes the scan nor replaces the real commit."""
        for bogus in ("NOTINT", "123"):
            r = self._repo(f"\x1ea\x1fb\x1f{bogus}\x1f{bogus}\x1fe\x1ff\x1fg\x1f\n")
            d, _ = collect(r)
            self.assertEqual(d["meta"]["commits"], 2)
            self.assertEqual(by_number(d)[3]["lines"], 1)
            self.assertEqual(d["loc"]["days"][-1][1], 2)

    def test_nul_free_body_with_tabs_and_newlines_kept(self) -> None:
        """Ordinary bodies still parse: GitLab 'See merge request' references are found."""
        r = Repo()
        r.commit("chore: init", {"a.py": "a\n"})
        r.commit("Some change\n\nfeat: real title\n\tindented\n\nSee merge request grp/proj!42", {"a.py": "a\nb\n"})
        d, _ = collect(r)
        self.assertEqual(by_number(d)[42]["title"], "feat: real title")


class F2_GitLabDetailErrors(unittest.TestCase):
    """A failed per-MR GitLab lookup leaves values unknown instead of guessing."""

    MR = ms.gl_mr(1, "fix: small", BASE, merged=BASE + DAY)

    def _fetch(self, handler: ms.Handler) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Run fetch_gitlab against `handler` and return (raw PRs, info)."""
        info: dict[str, int] = {}
        env = helpers.clean_env({"GITLAB_TOKEN": FAKE})
        with mock.patch.object(rs, "_backoff", lambda s: None), ms.MockServer(handler) as srv:
            with rs.Cli("glab", "gitlab.com", {**env, "REPO_STATS_FAKE_URL": srv.url}) as cli:
                prs = rs.fetch_gitlab(cli, "g/p", None, "main", False, None, True, progress=lambda m: None, info=info)
        return prs, info

    def test_notes_failure_is_not_unreviewed(self) -> None:
        """A 404 on notes gives reviewed=None, no 'unreviewed' risk, and is counted in info."""
        base = ms.gitlab_handler([self.MR], {1: [ms.gl_commit(T0)]}, {})

        def h(req: ms.Request) -> ms.Response:
            """Fail notes; answer the rest as the GitLab mock."""
            return (404, {}, {"message": "nope"}) if req["path"].endswith("/notes") else base(req)

        prs, info = self._fetch(h)
        self.assertIsNone(prs[0]["reviewed"])
        self.assertIn("first_review", prs[0]["inferred"])
        self.assertEqual(prs[0]["first_commit"], T0)  # the commits lookup still worked
        self.assertEqual(info["detail_errors"], 1)
        rec = rs.build_records(prs, {"failure_re": re.compile(rs.DEFAULT_FAILURE, re.I), "failure_title_start": True,
                                     "bot_re": re.compile("x^"), "fp_by_sha": {}, "claimed": {}, "claims": {},
                                     "redact": False, "reverts": {}})[0]
        self.assertNotIn("unreviewed", [f[0] for f in rec["risk_factors"]])

    def test_detail_failure_warns_in_collect(self) -> None:
        """collect() lists failed lookups in the dashboard warnings."""
        r = api_repo("git@gitlab.com:grp/sub/proj.git")
        mrs, commits, notes = gitlab_fixture(r)
        base = ms.gitlab_handler(mrs, commits, notes, page_size=50)

        def h(req: ms.Request) -> ms.Response:
            """Fail every notes request with 500."""
            return (500, {}, {"message": "boom"}) if req["path"].endswith("/notes") else base(req)

        with mock.patch.object(rs, "_backoff", lambda s: None), ms.MockServer(h) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE})
        self.assertTrue(any("lookups fail" in w for w in d["meta"]["warnings"]))
        self.assertTrue(all(p["reviewed"] is None for p in d["prs"]))

    def test_commits_are_paginated(self) -> None:
        """MR commits are read across pages, so the first commit of a long MR is found."""
        newest_first = [ms.gl_commit(T0 + 1000 - i) for i in range(150)]
        base = ms.gitlab_handler([self.MR], {}, {})

        def h(req: ms.Request) -> ms.Response:
            """Serve 150 commits newest first in pages of 100."""
            if req["path"].endswith("/commits"):
                page = int(req["query"].get("page", ["1"])[0])
                return 200, {"X-Next-Page": "2" if page == 1 else ""}, newest_first[(page - 1) * 100: page * 100]
            return base(req)

        prs, info = self._fetch(h)
        self.assertEqual(prs[0]["commits"], 150)
        self.assertEqual(prs[0]["first_commit"], T0 + 1000 - 149)
        self.assertEqual(info["detail_errors"], 0)


class F3_Heuristics(unittest.TestCase):
    """Type precedence, revert detection and the default tag pattern."""

    def test_type_order_title_branch_label_keyword(self) -> None:
        """A label beats a keyword guess; the branch prefix beats a label; the title beats everything."""
        self.assertEqual(rs.classify_type("Add null check", "patch-1", ["bug"])[::3], ("fix", "label"))
        self.assertEqual(rs.classify_type("Add null check", "docs/x", ["bug"])[::3], ("docs", "branch"))
        self.assertEqual(rs.classify_type("perf: faster", "docs/x", ["bug"])[::3], ("perf", "title"))
        self.assertEqual(rs.classify_type("Add null check", "patch-1", [])[::3], ("feat", "keyword"))
        self.assertEqual(rs.classify_type("Add null check", "patch-1", ["type: fix"])[::3], ("fix", "label"))

    def test_revert_title_needs_a_word_boundary(self) -> None:
        """'Revert', 'Reverts' and 'Reverting' are reverts; 'Revertible' is not."""
        for t, want in [("Revert \"feat: x\"", True), ("Reverts #12", True), ("Reverting the cache", True),
                        ("Revertible settings toggle", False), ("feat: revert button", False)]:
            self.assertEqual(bool(rs.REVERT_TITLE.match(t)), want, t)

    def test_default_tag_pattern_excludes_prereleases(self) -> None:
        """Version tags count, semver or not; common pre-release and nightly tags do not."""
        rx = re.compile(rs.DEFAULT_TAG_PATTERN)
        # Non-semver releases count: a release does not need a dot.
        for t in ["v1.2.3", "1.2.3", "v1.2", "2024.05.01", "v1.2.3-hotfix1", "v1", "v12", "2024", "7"]:
            self.assertTrue(rx.search(t), t)
        for t in ["v1.0.0-rc1", "v2.0.0-beta", "1.0.0dev", "2.0.0-preview", "3.0.0-next.1", "v1.0.0-pre",
                  "1.0.0-SNAPSHOT", "v4.0.0-canary.3", "20240101-nightly", "1.0.0a1", "1.0.0b2", "v2-rc1",
                  "v3-beta", "release-1.0", "latest"]:
            self.assertFalse(rx.search(t), t)


class F4_CliAndServe(unittest.TestCase):
    """Argument validation, the served URL and security headers."""

    def test_max_prs_must_be_positive(self) -> None:
        """--max-prs 0 or below is rejected by argparse, not silently misapplied."""
        for v in ("0", "-1", "x"):
            p = run_cli([merge_repo().path, "--max-prs", v, "-o", tmpdir() / "o.html"])
            self.assertEqual(p.returncode, 2, v)
            self.assertIn("--max-prs", p.stderr)

    def test_serve_port_range(self) -> None:
        """An out-of-range --serve port is a usage error, not a traceback."""
        for v in ("-5", "70000"):
            p = run_cli([merge_repo().path, "--serve", v, "--no-open", "-o", tmpdir() / "o.html"])
            self.assertEqual(p.returncode, 2, v)
            self.assertNotIn("Traceback", p.stderr)

    def test_served_url_is_percent_encoded_and_has_headers(self) -> None:
        """A file name with '#' and a space is served at the printed URL, with nosniff and CSP headers."""
        srv = E2_Serve()
        out = tmpdir()
        proc = subprocess.Popen([sys.executable, SCRIPT, str(merge_repo().path), "-o", str(out / "my stats#1.html"),
                                 "--serve", "0", "--no-open"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=helpers.clean_env())
        try:
            url = None
            for _ in range(200):
                line = proc.stderr.readline()  # pyright: ignore[reportOptionalMemberAccess]
                m = re.search(r"Serving (\S+)", line)
                if m:
                    url = m.group(1)
                    break
                if not line:
                    break
            self.assertIsNotNone(url)
            self.assertTrue(url.endswith("/my%20stats%231.html"), url)  # pyright: ignore[reportOptionalMemberAccess]
            status, _, headers = srv._get(url)  # pyright: ignore[reportArgumentType]
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
            for s in (proc.stdout, proc.stderr):
                if s:
                    s.close()

    def test_template_has_csp_without_network(self) -> None:
        """The dashboard's CSP allows no network access and comes before the script."""
        html = (ROOT / "template.html").read_text(encoding="utf-8")
        m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html)
        self.assertIsNotNone(m)
        csp = m.group(1)  # pyright: ignore[reportOptionalMemberAccess]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("connect-src", csp)
        self.assertLess(m.start(), html.index("<script>"))  # pyright: ignore[reportOptionalMemberAccess]

    def test_csv_guard_covers_leading_whitespace(self) -> None:
        """A formula after leading spaces is neutralised too; ordinary text is untouched."""
        path = tmpdir() / "x.csv"
        rs.write_csv(str(path), [{"id": 1, "title": "  =HYPERLINK(\"http://x\")"}, {"id": 2, "title": "   "},
                                 {"id": 3, "title": "fix: ok"}])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["title"] for r in rows], ["'  =HYPERLINK(\"http://x\")", "   ", "fix: ok"])


# =====================================================================================================
# G. gh / glab: read-only requests, host trust and no credential handling
# =====================================================================================================

class G1_ReadOnlyCli(unittest.TestCase):
    """Every API request is a read-only gh/glab call to an explicit host, from outside the repository."""

    def _cli(self, tool: str, url: str = "http://127.0.0.1:1", **extra: str) -> rs.Cli:
        """A Cli for `tool` that uses the fake CLIs and the mock server at `url`; closed after the test."""
        cli = rs.Cli(tool, "github.com" if tool == "gh" else "gitlab.com",
                     helpers.clean_env({"REPO_STATS_FAKE_URL": url, **extra}))
        self.addCleanup(cli.close)
        return cli

    def test_graphql_refuses_mutations(self) -> None:
        """Mutations, subscriptions and anything that is not a query are refused before gh runs."""
        log = tmpdir() / "cli.log"
        cli = self._cli("gh", REPO_STATS_FAKE_LOG=str(log))
        for q in ("mutation { addStar(input:{starrableId:\"x\"}) { clientMutationId } }",
                  "query { a } mutation { b }", "subscription { x }", "{ viewer { login } }", ""):
            with self.assertRaises(rs.ApiError, msg=q):
                cli.graphql(q, {})
        self.assertEqual(helpers.cli_calls(log), [], "no gh process may start for a refused request")

    def test_gitlab_refuses_endpoints_outside_the_allowlist(self) -> None:
        """Only the three merge request list endpoints can be requested, and only with GET."""
        log = tmpdir() / "cli.log"
        cli = self._cli("glab", REPO_STATS_FAKE_LOG=str(log))
        for ep in ("projects/g%2Fp/merge_requests/1/approve", "projects/g%2Fp/merge_requests/1/merge",
                   "projects/g%2Fp", "user", "projects/g/p/merge_requests", "projects/g%2Fp/merge_requests/1",
                   "projects/g%2Fp/merge_requests?x=1", "graphql", "/projects/g%2Fp/merge_requests"):
            with self.assertRaises(rs.ApiError, msg=ep):
                cli.get(ep, {})
        with self.assertRaises(rs.ApiError):  # glab cannot send GraphQL, nor gh GitLab requests
            cli.graphql(rs.GITHUB_QUERY, {})
        with self.assertRaises(rs.ApiError):
            self._cli("gh").get("projects/g%2Fp/merge_requests", {})
        self.assertEqual(helpers.cli_calls(log), [])

    def test_source_has_no_write_requests(self) -> None:
        """The source sends no mutation and names no write method."""
        src = (ROOT / "repo_stats.py").read_text()
        self.assertTrue(rs.READ_ONLY_GRAPHQL.match(rs.GITHUB_QUERY))
        self.assertIsNone(rs.WRITE_GRAPHQL.search(rs.GITHUB_QUERY))
        for word in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"', '"-X"', '"-F"', '"--field"', '"--input"', '"--form"'):
            self.assertNotIn(word, src, word)
        git_subcommands = set(re.findall(r'run_git\(repo, "([a-z-]+)"', src) + re.findall(r'stream_git\(repo, "([a-z-]+)"', src))
        self.assertLessEqual(git_subcommands, {"log", "rev-parse", "for-each-ref", "symbolic-ref", "remote", "config"},
                             git_subcommands)
        self.assertEqual(re.findall(r'run_git\(repo, "config", "([^"]+)"', src), ["--get-regexp"], "git config only reads")

    def test_bad_hostnames_rejected(self) -> None:
        """--hostname must be a plain host name: no ports, schemes, paths, spaces or leading dashes."""
        r = api_repo()
        for bad in ("-x", "--hostname=evil", "evil.example:8443", "https://evil.example", "a b", "evil.example/x", ""):
            with self.assertRaises((rs.ToolError, SystemExit), msg=bad):
                collect(r, "--provider", "github", "--hostname", bad, env={"GITHUB_TOKEN": FAKE})

    def test_hostname_needs_provider_for_unknown_hosts(self) -> None:
        """--hostname for a host that is not github.com/gitlab.com needs --provider."""
        r = api_repo("git@git.corp.example:team/app.git")
        d, _ = collect(r, "--hostname", "git.corp.example", env={"GITLAB_TOKEN": FAKE})
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertTrue(any("needs --provider" in w for w in d["meta"]["warnings"]))

    def test_every_call_is_read_only_explicit_host_and_outside_the_repo(self) -> None:
        """Each gh/glab call has --hostname and --include, no write flags, and runs from a private temp dir."""
        for remote, provider_env, handler_for in (
                ("https://github.com/acme/widgets.git", "GITHUB_TOKEN",
                 lambda r: ms.github_handler(desc_nodes(github_nodes(r)), page_size=3)),
                ("git@gitlab.com:grp/sub/proj.git", "GITLAB_TOKEN",
                 lambda r: ms.gitlab_handler(*gitlab_fixture(r), page_size=2))):
            r = api_repo(remote)
            log = tmpdir() / "cli.log"
            with ms.MockServer(handler_for(r)) as srv:
                d, _ = collect(r, "--fake-api", srv.url, env={provider_env: FAKE, "REPO_STATS_FAKE_LOG": str(log)})
            self.assertEqual(d["meta"]["mode"], "api", remote)
            calls = helpers.cli_calls(log)
            self.assertGreater(len(calls), 1)
            for c in calls:
                argv = c["argv"]
                self.assertEqual(argv[0], "api")
                self.assertIn(argv[argv.index("--hostname") + 1], ("github.com", "gitlab.com"))
                self.assertIn("--include", argv)
                if c["tool"] == "glab":
                    self.assertEqual(argv[argv.index("--method") + 1], "GET")
                else:
                    self.assertEqual(argv[1], "graphql")
                    query = next(a for a in argv if a.startswith("query="))[len("query="):]
                    self.assertTrue(rs.READ_ONLY_GRAPHQL.match(query))
                self.assertEqual(Path(c["cwd"]).resolve().parent, Path(tempfile.gettempdir()).resolve())
                self.assertTrue(Path(c["cwd"]).name.startswith("repo-stats-cli-"), c["cwd"])
            # One private directory per run, and it is gone afterwards.
            self.assertEqual(len({c["cwd"] for c in calls}), 1)
            self.assertFalse(Path(calls[0]["cwd"]).exists())
            self.assertEqual(calls[0]["env"]["GIT_CEILING_DIRECTORIES"], tempfile.gettempdir())

    def test_cli_working_directory_is_private_and_empty(self) -> None:
        """The CLI's working directory is new, empty and mode 0700, so nobody can plant a .git in it."""
        with rs.Cli("gh", "github.com", helpers.clean_env()) as cli:
            cwd = Path(cli._cwd.name)
            self.assertEqual(list(cwd.iterdir()), [])
            if os.name == "posix":
                self.assertEqual(cwd.stat().st_mode & 0o777, 0o700)
        self.assertFalse(cwd.exists())

    def test_cli_environment_has_no_telemetry_or_debug_switches(self) -> None:
        """glab runs with telemetry off, and neither CLI gets request-dumping debug flags."""
        r = api_repo("git@gitlab.com:grp/sub/proj.git")
        log = tmpdir() / "cli.log"
        noisy = {"GH_DEBUG": "api", "DEBUG": "1", "GLAB_DEBUG": "true", "GLAB_DEBUG_HTTP": "true"}
        with ms.MockServer(ms.gitlab_handler(*gitlab_fixture(r), page_size=50)) as srv:
            collect(r, "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE, "REPO_STATS_FAKE_LOG": str(log), **noisy})
        calls = helpers.cli_calls(log)
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(c["env"].get("GLAB_SEND_TELEMETRY"), "false")
            for k in noisy:
                self.assertNotIn(k, c["env"])
            self.assertEqual(c["env"].get("GITLAB_TOKEN"), FAKE, "the CLI keeps its own login variables")

    def test_lookalike_remote_never_starts_a_cli(self) -> None:
        """A lookalike or unknown remote host starts no gh/glab process at all."""
        for url in ("https://github.evil.com/acme/widgets.git", "git@gitlab.evil.example:a/b.git",
                    "https://github.com.evil.example/a/b.git"):
            r = fixtures.merge_workflow_repo()
            add_remote(r, url)
            log = tmpdir() / "cli.log"
            d, _ = collect(r, env={"GITHUB_TOKEN": FAKE, "GITLAB_TOKEN": FAKE, "REPO_STATS_FAKE_LOG": str(log)})
            self.assertEqual(d["meta"]["mode"], "git", url)
            self.assertEqual(helpers.cli_calls(log), [], url)

    def test_ci_server_host_is_trusted_only_when_it_matches(self) -> None:
        """In GitLab CI, a remote on CI_SERVER_HOST uses glab without --hostname; any other host does not."""
        r = api_repo("https://git.corp.example/grp/proj.git")
        mrs, commits, notes = gitlab_fixture(r)
        ci = {"GITLAB_CI": "true", "GITLAB_TOKEN": FAKE}
        with ms.MockServer(ms.gitlab_handler(mrs, commits, notes, page_size=50)) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env={**ci, "CI_SERVER_HOST": "git.corp.example"})
            self.assertEqual(d["meta"]["mode"], "api")
            self.assertEqual(d["meta"]["provider"], "gitlab")
            n = len(srv.requests)
            d, _ = collect(r, "--fake-api", srv.url, env={**ci, "CI_SERVER_HOST": "other.example"})
            self.assertEqual(d["meta"]["mode"], "git")
            d, _ = collect(r, "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE, "CI_SERVER_HOST": "git.corp.example"})
            self.assertEqual(d["meta"]["mode"], "git", "CI_SERVER_HOST counts only inside GitLab CI")
            self.assertEqual(len(srv.requests), n)

    def test_git_never_sees_credential_variables(self) -> None:
        """git, and anything it runs, gets an allowlisted environment: no credentials, no unknown variables."""
        r = fixtures.merge_workflow_repo()
        creds = {name: f"secret-{i}" for i, name in enumerate(rs.CREDENTIAL_VARS)}
        extra = {"REPO_STATS_CANARY": "dropped", "ACTIONS_RUNTIME_TOKEN": "secret-actions", "LC_ALL": "C",
                 "GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent", "GIT_CONFIG_PARAMETERS": "'x.y'='z'"}
        with mock.patch.dict(os.environ, {**creds, **extra}):
            out = rs.run_git(str(r.path), "-c", "alias.envdump=!env", "envdump")
            streamed = "".join(rs.stream_git(str(r.path), "-c", "alias.envdump=!env", "envdump"))
        for text in (out, streamed):
            env = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            self.assertTrue(env.get("PATH", "").endswith(os.environ["PATH"]))  # git may prepend its exec dir
            self.assertEqual(env.get("LC_ALL"), "C")
            self.assertEqual(env.get("GIT_NO_LAZY_FETCH"), "1")
            for name in ("REPO_STATS_CANARY", "ACTIONS_RUNTIME_TOKEN", "GIT_WORK_TREE"):
                self.assertNotIn(name, env)
            # git exports its own -c options to the alias; the caller's injected ones must not be there.
            self.assertNotIn("'x.y'", env.get("GIT_CONFIG_PARAMETERS", ""))
            self.assertNotEqual(env.get("GIT_DIR"), "/nonexistent", "GIT_DIR from the caller must not reach git")
            for name, value in creds.items():
                self.assertNotIn(value, text, name)
        self.assertFalse(set(rs.CREDENTIAL_VARS) & rs.GIT_ENV_ALLOW)

    def test_parse_cli_response(self) -> None:
        """--include output splits into status, lower-cased headers and body, with CRLF or LF."""
        st, h, body = rs.parse_cli_response(b"HTTP/2.0 200 OK\r\nX-Next-Page: 2\r\nLink: <a>; rel=next\r\n\r\n[1, 2]")
        self.assertEqual((st, h["x-next-page"], h["link"], json.loads(body)), (200, "2", "<a>; rel=next", [1, 2]))
        st, h, body = rs.parse_cli_response(b"HTTP/1.1 404 Not Found\nContent-Type: application/json\n\n{\"message\": \"x\"}")
        self.assertEqual((st, h["content-type"], body), (404, "application/json", b'{"message": "x"}'))
        self.assertEqual(rs.parse_cli_response(b"HTTP/2.0 204 No Content\r\n\r\n")[::2], (204, b""))
        self.assertEqual(rs.parse_cli_response(b"error connecting to api.github.com")[0], None)
        self.assertEqual(rs.parse_cli_response(b"")[0], None)


class G2_GitLabJobToken(unittest.TestCase):
    """A login that cannot read MR commits (a GitLab CI job token) degrades to local commits, once."""

    def test_refused_commits_use_local_merge_commits(self) -> None:
        """One refused probe stops all commit lookups; merge-commit MRs get first-commit times from git."""
        r = api_repo("git@gitlab.com:grp/sub/proj.git")
        mrs, commits, notes = gitlab_fixture(r)
        inner = ms.gitlab_handler(mrs, commits, notes, page_size=50)

        def h(req: ms.Request) -> ms.Response:
            """Refuse every commits request with 403, as GitLab does for a job token; serve the rest."""
            if req["path"].endswith("/commits"):
                return 403, {}, {"message": "403 Forbidden"}
            return inner(req)

        with ms.MockServer(h) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE})
            commit_calls = [q for q in srv.requests if q["path"].endswith("/commits")]
        self.assertEqual(d["meta"]["mode"], "api")
        self.assertEqual(len(commit_calls), 1, "after one refusal, no more commit lookups")
        self.assertTrue(any("cannot read merge request commits" in w for w in d["meta"]["warnings"]))
        self.assertFalse(any("lookups fail" in w for w in d["meta"]["warnings"]))
        p1 = by_number(d)[1]  # merged with a local merge commit: first commit comes from the branch
        self.assertIsNotNone(p1["first_commit"])
        self.assertLessEqual(p1["first_commit"], p1["merged"])
        self.assertIn("lead", p1["inferred"])
        self.assertIn("coding", p1["inferred"])
        self.assertTrue(p1["reviewed"])  # notes still work with a job token
        self.assertIsNone(by_number(d)[4]["first_commit"])  # open, no local commit: stays unknown

    def test_404_on_the_probed_mr_does_not_stop_other_lookups(self) -> None:
        """A 404 on one MR's commits is about that MR, not the login: the other MRs still get commit data."""
        r = api_repo("git@gitlab.com:grp/sub/proj.git")
        mrs, commits, notes = gitlab_fixture(r)
        inner = ms.gitlab_handler(mrs, commits, notes, page_size=50)
        first: list[str] = []

        def h(req: ms.Request) -> ms.Response:
            """Answer 404 for the commits of the first MR asked about; serve everything else."""
            if req["path"].endswith("/commits"):
                if not first:
                    first.append(req["path"])
                if req["path"] == first[0]:
                    return 404, {}, {"message": "404 Not Found"}
            return inner(req)

        with ms.MockServer(h) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE})
            commit_paths = {q["path"] for q in srv.requests if q["path"].endswith("/commits")}
        self.assertEqual(d["meta"]["mode"], "api")
        self.assertGreater(len(commit_paths), 1, "lookups continue after a 404")
        self.assertFalse(any("cannot read merge request commits" in w for w in d["meta"]["warnings"]))
        self.assertTrue(any("1 merge request(s) had commit or review lookups fail" in w for w in d["meta"]["warnings"]),
                        d["meta"]["warnings"])

    def test_job_token_autologin_reaches_the_api(self) -> None:
        """With GLAB_ENABLE_CI_AUTOLOGIN in GitLab CI, glab authenticates with CI_JOB_TOKEN by itself."""
        r = api_repo("git@gitlab.com:grp/sub/proj.git")
        mrs, commits, notes = gitlab_fixture(r)
        inner = ms.gitlab_handler(mrs, commits, notes, page_size=50)
        seen = []

        def h(req: ms.Request) -> ms.Response:
            """Accept JOB-TOKEN like GitLab does; refuse commits, which a job token cannot read."""
            seen.append(req["headers"].get("job-token"))
            if req["path"].endswith("/commits"):
                return 403, {}, {"message": "403 Forbidden"}
            return inner({**req, "headers": {**req["headers"], "private-token": req["headers"].get("job-token")}})

        env = {"GITLAB_CI": "true", "GLAB_ENABLE_CI_AUTOLOGIN": "true", "CI_JOB_TOKEN": "job-tok-123"}
        with ms.MockServer(h) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env=env)
        self.assertEqual(d["meta"]["mode"], "api")
        self.assertEqual(set(seen), {"job-tok-123"})


class H1_ReviewCycles(unittest.TestCase):
    """Review cycles per PR: one cycle per reviewed version, from GitHub review commits or GitLab notes."""

    H = 3600

    def _gh(self, reviews: Sequence[tuple[Any, ...]], **kw: Any) -> dict[str, Any]:
        """Convert a merged mock GitHub PR by "alice" with `reviews` into a raw PR dict."""
        return rs.github_pr(ms.gh_node(1, "feat: x", BASE, merged=BASE + 20 * self.H, reviews=reviews, **kw))

    def _gl(self, notes: Sequence[dict[str, Any]], merged: int | None = BASE + 20 * 3600,
            want_reviews: bool = True) -> dict[str, Any]:
        """Run fetch_gitlab for one MR by "alice" with `notes` and return its raw PR dict."""
        mr = ms.gl_mr(1, "feat: x", BASE, merged=merged)
        env = helpers.clean_env({"GITLAB_TOKEN": FAKE})
        with ms.MockServer(ms.gitlab_handler([mr], {1: [ms.gl_commit(BASE)]}, {1: list(notes)})) as srv:
            with rs.Cli("glab", "gitlab.com", {**env, "REPO_STATS_FAKE_URL": srv.url}) as cli:
                return rs.fetch_gitlab(cli, "g/p", None, "main", False, None, want_reviews, progress=lambda m: None)[0]

    # -- the counting rule

    def test_count_rule(self) -> None:
        """Versions and pushes start cycles; repeat reviews of one version and unreviewed pushes do not."""
        c = rs.count_review_cycles
        self.assertEqual(c([]), (0, False))
        self.assertEqual(c([(1, "review", "a"), (2, "review", "a"), (3, "review", "a")]), (1, False))
        self.assertEqual(c([(1, "review", "a"), (2, "review", "b"), (3, "review", "b"), (4, "review", "c")]), (3, False))
        self.assertEqual(c([(1, "review", None), (2, "push", None), (3, "review", None), (4, "review", None)]), (2, False))
        self.assertEqual(c([(1, "push", None), (2, "push", None), (3, "review", None)]), (1, False))
        self.assertEqual(c([(1, "review", None), (2, "push", None)]), (1, False))  # unreviewed final push
        self.assertEqual(c([(3, "review", None), (1, "review", None), (2, "push", None)]), (2, False))  # any order
        self.assertEqual(c([(1, "review", "a"), (2, "review", None), (3, "review", "a")]), (1, False))  # unknown oid
        # Replies to an older review thread are reviews of the older commit: A, B, A, B is two versions.
        self.assertEqual(c([(1, "review", "a"), (2, "review", "b"), (3, "review", "a"), (4, "review", "b")]), (2, False))
        self.assertEqual(c([(1, "review", "a"), (2, "review", "b"), (3, "review", "a"), (4, "review", "c")]), (3, False))

    def test_github_replies_to_old_threads_add_no_cycles(self) -> None:
        """A real pattern (googleapis/librarian#7469): ten reviews of f4b2, then replies alternating with 91bc."""
        b, h = BASE, self.H
        oids = ["f4b2"] * 10 + ["91bc", "91bc", "f4b2", "91bc", "f4b2", "91bc"]
        p = self._gh([("bob" if i % 2 else "carol", "User", "COMMENTED", b + i * 60, oid) for i, oid in enumerate(oids)]
                     + [("bob", "User", "APPROVED", b + h, "91bc")])
        self.assertEqual(p["review_cycles"], 2)

    def test_cap(self) -> None:
        """Counting stops at the cap and says so."""
        events = [(i, "review", str(i)) for i in range(rs.REVIEW_CYCLE_CAP + 10)]
        self.assertEqual(rs.count_review_cycles(events), (rs.REVIEW_CYCLE_CAP, True))
        self.assertEqual(rs.count_review_cycles(events, cap=3), (3, True))

    # -- GitHub

    def test_github_scenarios(self) -> None:
        """The plan's scenarios, counted from the commit each review was made against."""
        b, h = BASE, self.H
        self.assertEqual(self._gh([])["review_cycles"], 0)
        self.assertEqual(self._gh([("bob", "User", "APPROVED", b + h, "c1")])["review_cycles"], 1)
        p = self._gh([("bob", "User", "CHANGES_REQUESTED", b + h, "c1"), ("bob", "User", "APPROVED", b + 3 * h, "c2")])
        self.assertEqual((p["review_cycles"], p["changes_requested"], p["reviewers"]), (2, 1, 1))
        p = self._gh([("bob", "User", "COMMENTED", b + h, "c1"), ("carol", "User", "CHANGES_REQUESTED", b + 2 * h, "c1"),
                      ("dave", "User", "COMMENTED", b + 3 * h, "c1"), ("bob", "User", "APPROVED", b + 5 * h, "c2")])
        self.assertEqual((p["review_cycles"], p["changes_requested"], p["reviewers"]), (2, 1, 3))
        self.assertEqual(p["inferred"], [])

    def test_github_force_push_is_a_new_version(self) -> None:
        """A rebased or force-pushed head has a new oid, so its review is a new cycle."""
        p = self._gh([("bob", "User", "APPROVED", BASE + self.H, "aaa"), ("bob", "User", "APPROVED", BASE + 2 * self.H, "bbb"),
                      ("bob", "User", "APPROVED", BASE + 3 * self.H, "ccc")])
        self.assertEqual(p["review_cycles"], 3)

    def test_github_ignored_reviews(self) -> None:
        """Author, bot, pending and post-merge reviews start no cycle."""
        b, h = BASE, self.H
        p = self._gh([("alice", "User", "COMMENTED", b + h, "c1"), ("ci[bot]", "Bot", "COMMENTED", b + 2 * h, "c2"),
                      ("carol", "User", "PENDING", b + 3 * h, "c3"), ("bob", "User", "APPROVED", b + 4 * h, "c4"),
                      ("bob", "User", "COMMENTED", b + 30 * h, "c5")])  # after the merge
        self.assertEqual((p["review_cycles"], p["reviewers"], p["changes_requested"]), (1, 1, 0))

    def test_github_truncated_reviews_are_a_lower_bound(self) -> None:
        """More reviews than one page returns marks the count as inferred."""
        p = self._gh([("bob", "User", "APPROVED", BASE + self.H, "c1")], review_total=150)
        self.assertIn("review_cycles", p["inferred"])

    def test_github_query_asks_for_review_commits(self) -> None:
        """The GraphQL query requests the reviewed commit and the review total, and stays read-only."""
        self.assertIn("commit{oid}", rs.GITHUB_QUERY)
        self.assertIn("reviews(first:100){totalCount", rs.GITHUB_QUERY)
        self.assertTrue(rs.READ_ONLY_GRAPHQL.match(rs.GITHUB_QUERY))
        self.assertFalse(rs.WRITE_GRAPHQL.search(rs.GITHUB_QUERY))

    def test_github_records_end_to_end(self) -> None:
        """Through collect(): fixture PR #1 had one outside review, #10 none, the open PR #6 none so far."""
        r = api_repo()
        with ms.MockServer(ms.github_handler(desc_nodes(github_nodes(r)), page_size=50)) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env={"GITHUB_TOKEN": FAKE})
        prs = by_number(d)
        self.assertEqual((prs[1]["review_cycles"], prs[1]["reviewers"]), (1, 1))
        self.assertEqual(prs[3]["changes_requested"], 1)
        self.assertEqual(prs[10]["review_cycles"], 0)
        self.assertFalse(prs[10]["reviewed"])
        self.assertEqual(prs[6]["review_cycles"], 0)
        self.assertEqual(d["meta"]["heuristics"]["review_cycle_cap"], rs.REVIEW_CYCLE_CAP)

    # -- GitLab

    def test_gitlab_pushes_split_cycles(self) -> None:
        """Comments and approvals between pushes are one cycle; "added N commits" notes are pushes."""
        b, h = BASE, self.H
        p = self._gl([ms.gl_push("alice", b + h), ms.gl_note("bob", b + 2 * h), ms.gl_note("carol", b + 3 * h),
                      ms.gl_push("alice", b + 4 * h, n=2), ms.gl_approval("bob", b + 5 * h),
                      ms.gl_push("alice", b + 6 * h)])  # unreviewed final push
        self.assertEqual((p["review_cycles"], p["reviewers"], p["first_review"]), (2, 2, b + 2 * h))
        self.assertIsNone(p["changes_requested"])
        self.assertTrue(p["reviewed"])

    def test_gitlab_approval_alone_is_a_review(self) -> None:
        """An approval with no comment is a review and one cycle."""
        p = self._gl([ms.gl_approval("bob", BASE + self.H)])
        self.assertEqual((p["review_cycles"], p["first_review"]), (1, BASE + self.H))
        self.assertTrue(p["reviewed"])

    def test_gitlab_ignored_notes(self) -> None:
        """Author and bot notes, other system notes and post-merge notes are not reviews."""
        b, h = BASE, self.H
        p = self._gl([ms.gl_note("alice", b + h), ms.gl_note("ci-bot", b + 2 * h, bot=True),
                      ms.gl_note("root", b + 3 * h, system=True, body="changed the description"),
                      ms.gl_approval("alice", b + 4 * h), ms.gl_note("bob", b + 30 * h)])
        self.assertEqual((p["review_cycles"], p["reviewers"]), (0, 0))
        self.assertFalse(p["reviewed"])

    def test_gitlab_no_reviews_leaves_cycles_unknown(self) -> None:
        """--no-reviews skips the notes, so cycles are unknown, not 0."""
        p = self._gl([ms.gl_note("bob", BASE + self.H)], want_reviews=False)
        self.assertIsNone(p["review_cycles"])
        self.assertIsNone(p["reviewers"])

    def test_gitlab_cap_stops_paging(self) -> None:
        """An MR with far more cycles than the cap is capped at it and marked inferred (a lower bound)."""
        b, notes = BASE, []
        for i in range(rs.REVIEW_CYCLE_CAP * 3):
            notes += [ms.gl_push("alice", b + 2 * i), ms.gl_note("bob", b + 2 * i + 1)]
        p = self._gl(notes, merged=b + 10 * DAY)
        self.assertEqual(p["review_cycles"], rs.REVIEW_CYCLE_CAP)
        self.assertIn("review_cycles", p["inferred"])

    # -- git mode and output

    def test_git_mode_has_no_cycles(self) -> None:
        """Git history has no reviews, so every record's cycles are None."""
        d = merge_data()
        self.assertTrue(d["prs"])
        self.assertTrue(all(p["review_cycles"] is None and p["reviewers"] is None for p in d["prs"]))

    def test_csv_columns(self) -> None:
        """The CSV has the three review columns, with blanks for unknown values."""
        recs = [{"id": 1, "review_cycles": 2, "changes_requested": 1, "reviewers": 2},
                {"id": 2, "review_cycles": None, "changes_requested": None, "reviewers": None}]
        path = tmpdir() / "out.csv"
        rs.write_csv(str(path), recs)
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["review_cycles"], r["changes_requested"], r["reviewers"]) for r in rows],
                         [("2", "1", "2"), ("", "", "")])


# =====================================================================================================
# I. Security hardening: outputs, environments, executables, parsing and paging
# =====================================================================================================

class I1_SecurityHardening(unittest.TestCase):
    """Regression tests for the findings of the security review."""

    def _cli(self, tool: str, host: str, environ: Mapping[str, str]) -> rs.Cli:
        """A Cli whose private directory is removed after the test."""
        cli = rs.Cli(tool, host, environ)
        self.addCleanup(cli.close)
        return cli

    # -- S1: outputs are written atomically and never through a symlink

    def test_write_output_refuses_symlinks(self) -> None:
        """An existing or dangling symlink at the output path is refused; its target is never touched."""
        d = tmpdir()
        victim = d / "victim.txt"
        victim.write_text("keep")
        (d / "link.html").symlink_to(victim)
        (d / "dangling.html").symlink_to(d / "created-by-attack")
        for name in ("link.html", "dangling.html"):
            with self.assertRaises(rs.ToolError, msg=name):
                rs.write_output(str(d / name), lambda f: f.write("pwned"))
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((d / "created-by-attack").exists())
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["dangling.html", "link.html", "victim.txt"])

    def test_write_output_is_atomic(self) -> None:
        """A write replaces the file in one step, leaves no temp file, and a failed write keeps the old file."""
        d = tmpdir()
        out = d / "o.html"
        out.write_text("old")
        rs.write_output(str(out), lambda f: f.write("new"))
        self.assertEqual(out.read_text(), "new")

        def boom(f: Any) -> None:
            """Write part of the output, then fail."""
            f.write("partial")
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            rs.write_output(str(out), boom)
        self.assertEqual(out.read_text(), "new")
        self.assertEqual([p.name for p in d.iterdir()], ["o.html"], "no temp file is left behind")

    def test_cli_refuses_symlinked_outputs(self) -> None:
        """-o, --json and --csv pointing at a symlink exit 1 with a clear error and leave the target alone."""
        for flag in ("-o", "--json", "--csv"):
            d = tmpdir()
            victim = d / "victim.txt"
            victim.write_text("keep")
            (d / "out.x").symlink_to(victim)
            args = [merge_repo().path, "-o", d / "real.html"] if flag != "-o" else [merge_repo().path]
            p = run_cli([*args, flag, d / "out.x"])
            self.assertEqual(p.returncode, 1, flag)
            self.assertIn("symbolic link", p.stderr, flag)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(victim.read_text(), "keep", flag)

    # -- N5: lone surrogates in API text cannot crash a write

    def test_lone_surrogates_are_written(self) -> None:
        """A lone surrogate is written as an escape: the dashboard JSON still decodes to the same string."""
        d = tmpdir()
        data = {"title": "a\ud800b"}
        rs.write_output(str(d / "x.html"), lambda f: f.write(rs.embed_json(data)))
        self.assertEqual(json.loads((d / "x.html").read_text(encoding="utf-8")), data)
        rs.write_csv(str(d / "x.csv"), [{"id": 1, "title": "t\udfffx"}])
        self.assertIn("\\udfff", (d / "x.csv").read_text(encoding="utf-8"))

    # -- S4, N9: the gh/glab environment is an allowlist

    def test_cli_environment_is_an_allowlist(self) -> None:
        """Host overrides, TLS switches and unknown variables are dropped; login, CI, proxy and config stay."""
        keep = {"GITLAB_TOKEN": FAKE, "GH_TOKEN": FAKE, "GH_ENTERPRISE_TOKEN": FAKE, "CI_JOB_TOKEN": "job",
                "GLAB_ENABLE_CI_AUTOLOGIN": "true", "GITLAB_CI": "true", "CI_SERVER_FQDN": "gitlab.com",
                "HOME": "/home/u", "XDG_CONFIG_HOME": "/home/u/.config", "GH_CONFIG_DIR": "/c/gh",
                "GLAB_CONFIG_DIR": "/c/glab", "https_proxy": "http://proxy:3128", "SSL_CERT_FILE": "/ca.pem",
                "LANG": "C.UTF-8", "LC_ALL": "C", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
        drop = {"GITLAB_HOST": "evil.example", "GL_HOST": "evil.example", "GITLAB_URI": "evil.example",
                "GITLAB_API_HOST": "evil.example", "GLAB_API_PROTOCOL": "http", "API_PROTOCOL": "http",
                "GH_HOST": "evil.example", "GH_REPO": "evil/repo", "GLAB_SKIP_TLS_VERIFY": "true",
                "GH_DEBUG": "api", "GLAB_DEBUG_HTTP": "true", "AWS_SECRET_ACCESS_KEY": "s", "GIT_DIR": "/x",
                "GLAB_CHECK_UPDATE": "true"}
        for tool, host in (("glab", "gitlab.com"), ("gh", "github.com")):
            env = self._cli(tool, host, helpers.clean_env({**keep, **drop})).env
            for k, v in keep.items():
                self.assertEqual(env.get(k), v, (tool, k))
            for k in drop:
                if k not in ("GITLAB_HOST", "GLAB_CHECK_UPDATE"):
                    self.assertNotIn(k, env, (tool, k))
            self.assertEqual(env["GH_NO_UPDATE_NOTIFIER"], "1")
            self.assertEqual(env["GLAB_CHECK_UPDATE"], "false")
            self.assertEqual(env["GLAB_SEND_TELEMETRY"], "false")
            self.assertEqual(env["GH_TELEMETRY"], "false")
        self.assertEqual(self._cli("glab", "git.corp.example", helpers.clean_env(drop)).env["GITLAB_HOST"],
                         "git.corp.example", "glab's GITLAB_HOST is always the --hostname host")
        self.assertNotIn("GITLAB_HOST", self._cli("gh", "github.com", helpers.clean_env(drop)).env)

    def test_cli_never_sees_host_overrides_end_to_end(self) -> None:
        """A real run with GITLAB_API_HOST and friends set still sends glab only the trusted host."""
        r = api_repo("git@gitlab.com:grp/sub/proj.git")
        log = tmpdir() / "cli.log"
        evil = {"GITLAB_API_HOST": "evil.example", "GL_HOST": "evil.example", "GITLAB_URI": "evil.example"}
        with ms.MockServer(ms.gitlab_handler(*gitlab_fixture(r), page_size=50)) as srv:
            d, _ = collect(r, "--fake-api", srv.url, env={"GITLAB_TOKEN": FAKE, "REPO_STATS_FAKE_LOG": str(log), **evil})
        self.assertEqual(d["meta"]["mode"], "api")
        calls = helpers.cli_calls(log)
        self.assertTrue(calls)
        for c in calls:
            for k in evil:
                self.assertNotIn(k, c["env"])
            self.assertEqual(c["env"]["GITLAB_HOST"], "gitlab.com")

    # -- S2: executables are found by absolute path, never in the current directory

    def test_find_executable_skips_current_and_relative_dirs(self) -> None:
        """A `gh` in the current directory is never used through an empty, "." or relative PATH entry."""
        d = tmpdir()
        (d / "sub").mkdir()
        for p in (d / "gh", d / "sub" / "gh"):
            p.write_text("#!/bin/sh\necho planted\n")
            p.chmod(0o755)
        old = os.getcwd()
        os.chdir(d)
        try:
            for path in ("", ".", "sub", os.pathsep.join(["", ".", "sub", "./sub"])):
                self.assertIsNone(rs.find_executable("gh", path), repr(path))
            with self.assertRaises(rs.CliUnavailable):
                rs.Cli("gh", "github.com", {"PATH": os.pathsep.join([".", "sub"])})
            self.assertEqual(rs.find_executable("gh", os.pathsep.join([".", str(d / "sub")])), str(d / "sub" / "gh"))
        finally:
            os.chdir(old)
        self.assertTrue(os.path.isabs(rs.git_cmd(".")[0]))

    # -- N1: no user info from a remote URL reaches a name or an output

    def test_parse_remote_drops_user_info(self) -> None:
        """Credentials in a remote URL never end up in the host or path, also without a path."""
        cases = {
            "https://user:SECRET@github.com": None,
            "https://user:SECRET@github.com/": None,
            "https://SECRET@github.com/acme/widgets.git": ("github.com", "acme/widgets"),
            "https://u:p@ss@git.corp.example/a/b.git": ("git.corp.example", "a/b"),
            "https://github.com@evil.example/o/r": ("evil.example", "o/r"),
            "ssh://git@github.com:evil/o/r": None,
        }
        for url, want in cases.items():
            got = rs.parse_remote(url)
            self.assertEqual(got, want, url)
            self.assertNotIn("SECRET", repr(got), url)

    def test_token_in_pathless_remote_is_not_in_the_output(self) -> None:
        """A path-less https remote with a token gives no API and no name from the remote."""
        r = fixtures.merge_workflow_repo()
        add_remote(r, "https://user:SECRET-TOKEN@github.com")
        d, _ = collect(r)
        self.assertEqual(d["meta"]["mode"], "git")
        self.assertNotIn("SECRET-TOKEN", json.dumps(d))

    # -- N2, N3: anchored regexes, no dot segments

    def test_security_regexes_match_the_whole_string(self) -> None:
        """A trailing newline, an empty label, dot segments and non-ASCII digits are all rejected."""
        for bad in ("ghe.example.com\n", "a..b", "ghe..example.com"):
            self.assertIsNone(rs.HOSTNAME_RE.match(bad), repr(bad))
        self.assertIsNotNone(rs.HOSTNAME_RE.match("ghe.example.com"))
        for bad in ("projects/g%2Fp/merge_requests\n", "projects/../merge_requests", "projects/./merge_requests",
                    "projects/../merge_requests/1/notes", "projects/g%2Fp/merge_requests/\u0661/notes"):
            self.assertIsNone(rs.GITLAB_READ_ENDPOINT.match(bad), repr(bad))
        for good in ("projects/g%2Fp/merge_requests", "projects/g%2Fp/merge_requests/12/commits", "projects/...x/merge_requests"):
            self.assertIsNotNone(rs.GITLAB_READ_ENDPOINT.match(good), good)
        log = tmpdir() / "cli.log"
        cli = self._cli("glab", "gitlab.com", helpers.clean_env({"REPO_STATS_FAKE_LOG": str(log)}))
        with self.assertRaises(rs.ApiError):
            cli.get("projects/../merge_requests", {})
        self.assertEqual(helpers.cli_calls(log), [])
        with self.assertRaises(rs.ToolError):
            collect(api_repo(), "--provider", "github", "--hostname", "ghe.example.com\n")

    # -- N4: paging is capped and validated

    def test_next_page_must_be_a_plain_increasing_number(self) -> None:
        """X-Next-Page must be a plain positive integer above the current page."""
        self.assertEqual(rs.next_page("2", 1), 2)
        for bad in ("-1", "+4", "1_0", "0", "1", "abc", "\u0662", "2.0", "99999999999"):
            with self.assertRaises(rs.ApiError, msg=bad):
                rs.next_page(bad, 1)

    def test_gitlab_repeating_next_page_stops(self) -> None:
        """A server that always answers X-Next-Page: 1 fails the fetch instead of looping forever."""
        def h(req: ms.Request) -> ms.Response:
            """Return one MR per page and always point back at page 1."""
            return 200, {"X-Next-Page": "1"}, [{"iid": 1, "title": "t", "state": "opened", "created_at": "2024-01-01T00:00:00Z"}]

        with ms.MockServer(h) as srv:
            cli = self._cli("glab", "gitlab.com", helpers.clean_env({"REPO_STATS_FAKE_URL": srv.url, "GITLAB_TOKEN": FAKE}))
            with self.assertRaises(rs.ApiError):
                rs.fetch_gitlab(cli, "g/p", None, "main", True, None, False, progress=lambda m: None)
            self.assertEqual(len(srv.requests), 1)

    def test_github_repeating_cursor_and_page_cap(self) -> None:
        """A repeating GraphQL cursor fails the fetch, and an endless one stops at MAX_LIST_PAGES."""
        counter = [0]

        def h(repeat: bool) -> ms.Handler:
            """A GraphQL handler that always has a next page, with a fixed or ever-changing cursor."""
            def inner(req: ms.Request) -> ms.Response:
                """Answer with no PRs and another page."""
                counter[0] += 1
                cursor = "same" if repeat else f"c{counter[0]}"
                return 200, {}, {"data": {"repository": {"pullRequests": {
                    "pageInfo": {"hasNextPage": True, "endCursor": cursor}, "nodes": []}}}}
            return inner

        env = {"GH_TOKEN": FAKE}
        with ms.MockServer(h(True)) as srv:
            cli = self._cli("gh", "github.com", helpers.clean_env({**env, "REPO_STATS_FAKE_URL": srv.url}))
            with self.assertRaisesRegex(rs.ApiError, "repeating"):
                rs.fetch_github(cli, "o", "r", None, "main", True, None, progress=lambda m: None)
            self.assertEqual(len(srv.requests), 2)
        with ms.MockServer(h(False)) as srv, mock.patch.object(rs, "MAX_LIST_PAGES", 3):
            cli = self._cli("gh", "github.com", helpers.clean_env({**env, "REPO_STATS_FAKE_URL": srv.url}))
            with self.assertRaisesRegex(rs.ApiError, "Stopped after 3 pages"):
                rs.fetch_github(cli, "o", "r", None, "main", True, None, progress=lambda m: None)
            self.assertEqual(len(srv.requests), 3)

    # -- N7: --serve headers

    def test_serve_sends_framing_and_cross_origin_headers(self) -> None:
        """--serve forbids framing in the header CSP and sends same-origin CORP and COOP."""
        out = tmpdir()
        proc = subprocess.Popen([sys.executable, SCRIPT, str(merge_repo().path), "-o", str(out / "o.html"),
                                 "--serve", "0", "--no-open"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, env=helpers.clean_env())
        try:
            url = None
            for line in proc.stderr:  # pyright: ignore[reportOptionalIterable]
                m = re.search(r"Serving (\S+)", line)
                if m:
                    url = m.group(1)
                    break
            self.assertIsNotNone(url)
            status, _, headers = E2_Serve()._get(url)  # pyright: ignore[reportArgumentType]
            self.assertEqual(status, 200)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
            if proc.stderr:
                proc.stderr.close()

    # -- S5, S6: git hardening and partial clones

    def test_git_argv_is_hardened(self) -> None:
        """Every git call disables fsmonitor and fetching; the numstat log disables textconv and ext diff."""
        argv = rs.git_cmd("/r", "log")
        for opt in ("core.fsmonitor=false", "protocol.allow=never"):
            self.assertEqual(argv[argv.index(opt) - 1], "-c", opt)
        src = (ROOT / "repo_stats.py").read_text()
        m = re.search(r'stream_git\(repo, "log", "--first-parent", "-m", "--numstat".*?\)', src, re.S)
        self.assertIsNotNone(m)
        self.assertIn('"--no-textconv"', m.group(0))  # pyright: ignore[reportOptionalMemberAccess]
        self.assertIn('"--no-ext-diff"', m.group(0))  # pyright: ignore[reportOptionalMemberAccess]

    def test_partial_clone_warns_and_never_fetches(self) -> None:
        """A blobless clone is detected; the run warns, fetches nothing, and fails with a clear hint."""
        src = merge_repo().path
        dest = tmpdir() / "partial"
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--upload-pack",
                        "git -c uploadpack.allowFilter=true upload-pack", f"file://{src}", str(dest)],
                       check=True, capture_output=True)

        def missing() -> int:
            """Count objects the partial clone does not have."""
            out = git_out(dest, "rev-list", "--objects", "--missing=print", "--all")
            return sum(1 for line in out.splitlines() if line.startswith("?"))

        before = missing()
        self.assertGreater(before, 0)
        self.assertTrue(rs.is_partial_clone(str(dest)))
        self.assertFalse(rs.is_partial_clone(str(src)))
        p = run_cli([dest, "-o", tmpdir() / "o.html"])
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("warning: This is a partial clone", p.stderr)
        self.assertIn("Clone without a filter", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(missing(), before, "no missing object may be fetched")

    # -- N6, N11: refs and PR numbers are never options or regex syntax

    def test_refs_starting_with_dash_are_refused(self) -> None:
        """A branch that looks like an option is refused before git sees it; no file is created."""
        d = tmpdir()
        p = run_cli([merge_repo().path, "-o", d / "o.html", "--branch=--output=pwned.txt"], cwd=d)
        self.assertEqual(p.returncode, 1)
        self.assertIn("starts with '-'", p.stderr)
        self.assertFalse((d / "pwned.txt").exists())

    def test_end_of_options_depends_on_git_version(self) -> None:
        """--end-of-options is used from git 2.30 (rev-parse support) and left out before."""
        with mock.patch.object(rs, "_git_version", (2, 30)):
            self.assertEqual(rs.revs("main"), ["--end-of-options", "main"])
        with mock.patch.object(rs, "_git_version", (2, 29)):
            self.assertEqual(rs.revs("main"), ["main"])
        self.assertGreaterEqual(rs.git_version(), (2, 0))
        # And the real git accepts the argv repo-stats builds.
        out = rs.run_git(str(merge_repo().path), "rev-parse", "--verify", "-q", *rs.revs("main^{commit}"))
        self.assertRegex(out.strip(), r"^[0-9a-f]{40}$")

    def test_pr_number_is_escaped_in_regex(self) -> None:
        """A PR number is matched literally in a squash subject, never as a regex."""
        c = {"sha": "abc", "parents": ["p"], "subject": "fix: thing (#11)"}
        self.assertIsNone(rs.local_summary({"number": "1+", "sha": "abc"}, {"abc": c}))  # as a regex, 1+ matches 11
        self.assertIs(rs.local_summary({"number": 11, "sha": "abc"}, {"abc": c}), c)


if __name__ == "__main__":
    unittest.main()
