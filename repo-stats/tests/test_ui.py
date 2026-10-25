"""Browser tests for the generated dashboard (plan section D). Needs node + playwright + chromium.

All browser work is done in one node run (ui_runner.js) in setUpClass; the tests below assert on its results.
The module skips cleanly when node, the playwright package or chromium is unavailable.

Set REPO_STATS_SHOTS_DIR to keep the full-page screenshots (default: a temp dir).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import add_remote, collect, rs
import fixtures
from fixtures import DAY, Repo, lines
import mock_servers as ms

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "ui_runner.js"
FAKE = "fake-token-ui-0000"
TYPE_GROUPS = ["feat", "fix", "refactor", "docs", "test", "chore", "perf", "other"]
GROUP_OF = {"style": "chore", "build": "chore", "ci": "chore", "revert": "other"}
EVIL = '</script><img src=x onerror=alert(1)> "q" \'s\' & <b class=pwn>b</b>'


def group_of(t):
    return t if t in TYPE_GROUPS else GROUP_OF.get(t, "other")


# ----------------------------------------------------------------------------------- independent maths

def percentile(v, p):
    v = sorted(v)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    r = p / 100 * (len(v) - 1)
    lo, hi = math.floor(r), math.ceil(r)
    return v[lo] + (v[hi] - v[lo]) * (r - lo)


def stat_row(vals):
    v = sorted(x for x in vals if x is not None)
    n = len(v)
    if not n:
        return None
    mean = sum(v) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": mean, "median": percentile(v, 50), "p75": percentile(v, 75), "p90": percentile(v, 90),
            "p95": percentile(v, 95), "p99": percentile(v, 99), "min": v[0], "max": v[-1], "sd": sd}


COLS = ["n", "mean", "median", "p75", "p90", "p95", "p99", "min", "max", "sd"]
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "mo": 30.44 * 86400}


def parse_num(cell):
    cell = cell.strip()
    if cell in ("–", ""):
        return None
    return float(cell.replace(",", ""))


def parse_dur(cell):
    cell = cell.strip()
    if cell in ("–", ""):
        return None, 1
    m = re.fullmatch(r"(-?[\d,]*\.?\d+)(mo|s|m|h|d)", cell)
    assert m, cell
    unit = UNITS[m.group(2)]
    return float(m.group(1).replace(",", "")) * unit, unit


def utc(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def day_start(t):
    return t - t % DAY


def week_start(t):
    d = utc(t)
    return day_start(t) - d.weekday() * DAY


def month_start(t):
    d = utc(t)
    return int(dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp())


def next_bucket(t, kind):
    if kind == "day":
        return t + DAY
    if kind == "week":
        return t + 7 * DAY
    d = utc(t)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(dt.datetime(y, m, 1, tzinfo=dt.timezone.utc).timestamp())


def bucket_start(t, kind):
    return {"day": day_start, "week": week_start, "month": month_start}[kind](t)


def data_range(data):
    times = [p["merged"] or p["created"] for p in data["prs"] if (p["merged"] or p["created"])]
    times += [p["closed"] for p in data["prs"] if p["closed"]]
    times += [d[0] for d in data["loc"]["days"]]
    times += [d["t"] for d in data["deploys"]]  # a release tagged after the last merge is part of "All time"
    return min(times), max(times)


def js_range(data, steps):
    lo_all, hi_all = data_range(data)
    preset, frm, to = "all", None, None
    for s in steps:
        if s["op"] == "preset":
            preset = s["value"]
        elif s["op"] == "custom":
            preset = "custom"
            frm = int(dt.datetime.strptime(s["from"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
            to = int(dt.datetime.strptime(s["to"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
    lo, hi = lo_all, hi_all + DAY - 1
    months = {"12m": 12, "6m": 6, "3m": 3, "1m": 1}.get(preset)
    if months:
        d = utc(hi_all)
        total = d.year * 12 + d.month - 1 - months
        y, m = divmod(total, 12)
        last = (dt.datetime(y + (m + 1) // 12, (m + 1) % 12 + 1, 1) - dt.timedelta(days=1)).day
        lo = int(dt.datetime(y, m + 1, min(d.day, last), tzinfo=dt.timezone.utc).timestamp())  # clamp the day
    if preset == "custom":
        lo = frm if frm is not None else lo
        hi = to + DAY - 1 if to is not None else hi
    return max(lo, lo_all), min(hi, hi_all + DAY)


def selection(data, steps):
    lo, hi = js_range(data, steps)
    types = set(TYPE_GROUPS)
    bots = True
    for s in steps:
        if s["op"] == "type" and s["value"] == "__all__":
            types = set()
        elif s["op"] == "type":
            types ^= {s["value"]}
        elif s["op"] == "bots":
            bots = s["value"]
    return [p for p in data["prs"] if p["state"] == "merged" and group_of(p["type"]) in types and (bots or not p["bot"])
            and lo <= p["merged"] <= hi], (lo, hi)


def period_counts(times, kind, lo, hi):
    out = []
    idx = {}
    t = bucket_start(lo, kind)
    while t <= hi:
        idx[t] = len(out)
        out.append([t, 0])
        t = next_bucket(t, kind)
    for x in times:
        i = idx.get(bucket_start(x, kind))
        if i is not None:
            out[i][1] += 1
    return out


def trimmed_period_counts(times, kind, lo, hi):
    b = period_counts(times, kind, lo, hi)
    if kind != "day" and len(b) > 2:
        if b[0][0] < lo:
            b = b[1:]
        if b and next_bucket(b[-1][0], kind) - 1 > hi:
            b = b[:-1]
    return [x[1] for x in b]


# ----------------------------------------------------------------------------------- dataset builders

def build_api_data():
    base_path = helpers.cached_synthetic(150)
    work = Path(tempfile.mkdtemp(prefix="repostats-uiapi-")) / "repo"
    shutil.copytree(base_path, work, symlinks=True)
    git_data, _ = collect(work)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", "https://github.com/acme/widgets.git"], check=True)
    rnd = random.Random(11)
    nodes = []
    for i, p in enumerate(git_data["prs"]):
        n = p["number"]
        merged = p["merged"]
        created = merged - int(rnd.lognormvariate(10.5, 1.0))
        first_commit = created - int(rnd.uniform(600, 90000))
        ready = created + int(rnd.choice([0, 0, 0, 1800, 20000]))
        state = "MERGED"
        author, atype = "alice", "User"
        if i % 10 == 3:
            author, atype = "dependabot[bot]", "Bot"
        reviews = []
        if rnd.random() < 0.85:
            reviews = [("bob", "User", "APPROVED", min(merged - 60, max(ready + 60, ready + int(rnd.expovariate(1 / 20000)))))]
        title = p["title"]
        if i == 7:
            title = "feat!: " + EVIL
        node = ms.gh_node(n, title, created, merged=merged, first_commit=first_commit, ready=ready if ready != created else None,
                          additions=p["additions"], deletions=p["deletions"], files=p["files"], commits=p["commits"] or 1,
                          author=author, author_type=atype, reviews=reviews, labels=["x" + EVIL] if i == 7 else [])
        if i % 17 == 5:
            node = ms.gh_node(n, title, created, state="OPEN", author=author, author_type=atype, additions=5, deletions=1, files=1)
        elif i % 13 == 9:
            node = ms.gh_node(n, title, created, closed=created + 7200, state="CLOSED", author=author, author_type=atype,
                              additions=5, deletions=1, files=1)
        nodes.append(node)
    nodes.sort(key=lambda n: n["createdAt"], reverse=True)
    with ms.MockServer(ms.github_handler(nodes, page_size=40)) as srv:
        data, _ = collect(work, "--api-url", srv.url, env={"GITHUB_TOKEN": FAKE})
    return data


def build_datasets(tmp):
    sets = {}

    def add(name, data):
        path = tmp / f"{name}.html"
        path.write_text(rs.render_html(data), encoding="utf-8")
        sets[name] = {"name": name, "html": str(path), "data": data}

    add("merge", collect(helpers.cached_synthetic(150))[0])
    add("squash", collect(helpers.cached_synthetic(150, "squash"))[0])
    add("api", build_api_data())
    r = Repo()
    r.commit("chore: init", {"a.py": lines(3)})
    r.commit("fix: direct", {"a.py": lines(4)})
    add("empty", collect(r)[0])
    r = Repo()
    r.commit("chore: init", {"a.py": lines(3)})
    r.squash_pr(1, "feat: the only one", {"b.py": lines(30)})
    add("single", collect(r)[0])
    r = Repo()
    r.commit("chore: initial commit", {"a.py": lines(5)})
    for i in range(1, 8):
        r.squash_pr(i, f"fix: small {i}", {f"s{i}.py": lines(3 + i)})
        r.advance(DAY)
    r.merge_pr(8, "feat/" + EVIL.replace(" ", "-").replace('"', "").replace("'", "")[:40], "feat!: " + EVIL,
               [("wip", {"src/auth/big.py": lines(3000)})])
    add("evil", collect(r, "--title", EVIL)[0])
    r = Repo()
    r.commit("chore: init", {"a.py": lines(3)})
    for i in range(1, 6):
        r.squash_pr(i, f"fix: thing {i}", {f"t{i}.py": lines(5 * i)})
        r.advance(DAY)
    add("notags", collect(r)[0])
    # release tag created after the last merge (All-time view must still count it)
    r = Repo()
    r.commit("chore: initial commit", {"a.py": lines(5)})
    for i in range(1, 6):
        r.squash_pr(i, f"fix: thing {i}", {f"t{i}.py": lines(5 * i)})
        r.advance(DAY)
    r.advance(20 * DAY)
    r.tag("v1.0.0", annotated=True)
    add("latetag", collect(r)[0])
    # some PRs released, the newest one not yet: KPI and DORA lead time must agree
    r = fixtures.merge_workflow_repo()
    r.advance(DAY)
    r.merge_pr(6, "feat/after-tag", "feat: after last tag", [("w", {"src/z.py": lines(9)})])
    add("mixedlead", collect(r)[0])
    return sets


def scenarios(sets):
    sc = [
        ("all", []),
        ("12m", [{"op": "preset", "value": "12m"}]),
        ("6m", [{"op": "preset", "value": "6m"}]),
        ("3m", [{"op": "preset", "value": "3m"}]),
        ("1m", [{"op": "preset", "value": "1m"}]),
        ("custom", [{"op": "custom", "from": "2022-03-01", "to": "2022-06-30"}]),
        ("custom-before-data", [{"op": "custom", "from": "2000-01-01", "to": "2000-01-02"}]),
        ("custom-inverted", [{"op": "custom", "from": "2022-06-30", "to": "2022-03-01"}]),
        ("one-type-off", [{"op": "type", "value": "feat"}]),
        ("two-types-off+6m", [{"op": "type", "value": "fix"}, {"op": "type", "value": "chore"}, {"op": "preset", "value": "6m"}]),
        ("all-types-off", [{"op": "type", "value": "__all__"}]),
        ("bots-off", [{"op": "bots", "value": False}], ["api"]),
        ("bots-off+custom", [{"op": "bots", "value": False}, {"op": "custom", "from": "2022-02-01", "to": "2022-09-30"}], ["api"]),
        ("period-month", [{"op": "period", "value": "month"}]),
    ]
    return [{"name": x[0], "steps": x[1], **({"only": x[2]} if len(x) > 2 else {})} for x in sc]


class UiTests(unittest.TestCase):
    R: dict = {}
    sets: dict = {}

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is not installed")
        probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, text=True, cwd=str(HERE))
        if probe.returncode != 0:
            raise unittest.SkipTest("the playwright node package is not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="repostats-ui-"))
        cls.sets = build_datasets(cls.tmp)
        shots = os.environ.get("REPO_STATS_SHOTS_DIR") or str(cls.tmp / "shots")
        Path(shots).mkdir(parents=True, exist_ok=True)
        cfg = {
            "datasets": [{"name": v["name"], "html": v["html"]} for v in cls.sets.values()],
            "main": "merge", "filterDatasets": ["merge", "api"], "scenarios": scenarios(cls.sets),
            "statsDatasets": ["merge", "squash", "api", "latetag", "mixedlead"], "shotDatasets": ["merge", "squash", "api", "empty", "evil"],
            "xssDatasets": ["evil", "api"], "shotsDir": shots, "results": str(cls.tmp / "results.json"),
            "sections": ["load", "theme", "controls", "filters", "stats", "tooltips", "tables", "banners", "a11y", "shots", "resize", "xss"],
        }
        (cls.tmp / "cfg.json").write_text(json.dumps(cfg))
        p = subprocess.run(["node", str(RUNNER), str(cls.tmp / "cfg.json")], capture_output=True, text=True, cwd=str(HERE), timeout=900)
        if p.returncode != 0:
            msg = p.stderr[-1500:]
            if "Executable doesn't exist" in msg or "browserType.launch" in msg and "install" in msg:
                raise unittest.SkipTest("chromium is not available for playwright")
            raise RuntimeError("ui runner failed:\n" + msg)
        cls.R = json.loads((cls.tmp / "results.json").read_text())
        cls.shots = shots

    # ------------------------------------------------------------------ D1
    def test_d1_loads_without_console_or_page_errors(self):
        for row in self.R["load"]:
            with self.subTest(row["ds"], theme=row["theme"], width=row["width"]):
                self.assertEqual(row["errors"], [])
                self.assertEqual(row["pageErrors"], [])
                self.assertEqual(row["dialogs"], [])
                self.assertEqual(row["sections"], [], "missing section ids")
                self.assertEqual(row["themeAttr"], row["theme"])

    def test_d1_no_horizontal_scroll(self):
        for row in self.R["load"]:
            with self.subTest(row["ds"], theme=row["theme"], width=row["width"]):
                self.assertLessEqual(row["hscroll"], 0, "page scrolls horizontally")
                self.assertEqual(row["overflowing"], [])

    def test_d1_no_network_requests(self):
        for row in self.R["load"]:
            self.assertEqual(row["attempts"], [], row["ds"])

    # ------------------------------------------------------------------ D2
    def test_d2_default_theme_dark_even_when_os_is_light(self):
        step = self.R["theme"]["steps"][0]
        self.assertEqual(step["theme"], "dark")
        self.assertEqual(step["surface"], "#1a1a19")
        self.assertEqual(self.R["themeOsDark"], "dark")

    def test_d2_switch_persist_and_rerender(self):
        s = {x["label"]: x for x in self.R["theme"]["steps"]}
        dark, light = s["initial (OS light, nothing stored)"], s["after click 1"]
        self.assertEqual(light["theme"], "light")
        self.assertEqual(light["surface"], "#fcfcfb")
        self.assertNotEqual(dark["barFill"], light["barFill"])
        self.assertEqual(dark["barFill"], "#3987e5")
        self.assertEqual(light["barFill"], "#2a78d6")
        self.assertNotEqual(dark["bodyBg"], light["bodyBg"])
        self.assertEqual(light["stored"], "light")
        self.assertEqual(s["after reload"]["theme"], "light")
        self.assertEqual(s["after reload"]["surface"], "#fcfcfb")
        self.assertEqual(s["after click 2"]["theme"], "dark")
        self.assertEqual(s["after reload 2"]["theme"], "dark")
        self.assertEqual(s["after reload 2"]["barFill"], "#3987e5")
        self.assertEqual(self.R["theme"]["errors"], [])

    def test_d2_all_charts_use_new_palette(self):
        s = {x["label"]: x for x in self.R["theme"]["steps"]}
        dark_only = {"#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9", "#e66767"}
        light_only = {"#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6", "#e34948"}
        self.assertFalse(dark_only & set(s["after click 1"]["fills"]), "dark colors left over after switching to light")
        self.assertFalse(light_only & set(s["initial (OS light, nothing stored)"]["fills"]))
        self.assertTrue(light_only & set(s["after click 1"]["fills"]))
        self.assertNotEqual(s["initial (OS light, nothing stored)"]["chipDot"], s["after click 1"]["chipDot"])

    def test_d2_theme_button_state_text(self):
        s = {x["label"]: x for x in self.R["theme"]["steps"]}
        self.assertEqual(s["initial (OS light, nothing stored)"]["btn"], ["Dark theme", "true"])
        self.assertEqual(s["after click 1"]["btn"], ["Light theme", "false"])

    # ------------------------------------------------------------------ D3
    def test_d3_no_null_undefined_nan_text(self):
        for row in self.R["load"]:
            with self.subTest(row["ds"], theme=row["theme"], width=row["width"]):
                self.assertEqual(row["text"], [])

    def test_d3_no_bad_text_after_every_control(self):
        for row in self.R["controls"]:
            with self.subTest(row["ds"]):
                self.assertEqual(row["issues"], [])
                self.assertEqual(row["errors"] + row["pageErrors"], [])

    def test_d3_all_sections_have_cards(self):
        for row in self.R["load"]:
            if row["ds"] in ("empty",):
                continue
            self.assertGreater(row["cards"], 25, row["ds"])
            self.assertGreater(row["svgs"], 12, row["ds"])

    def test_d3_metric_selector_offers_expected_metrics(self):
        by = {r["ds"]: r for r in self.R["controls"]}
        self.assertIn("lead", by["merge"]["metricOptions"])
        self.assertNotIn("pickup", by["merge"]["metricOptions"])
        self.assertIn("pickup", by["api"]["metricOptions"])
        self.assertIn("open_to_merge", by["api"]["metricOptions"])

    # ------------------------------------------------------------------ D4
    def test_d4_filters_match_independent_counts(self):
        for item in self.R["filters"]:
            data = self.sets[item["ds"]]["data"]
            with self.subTest(item["ds"], scenario=item["name"]):
                self.assertNotIn("exception", item)
                self.assertEqual(item["errors"], [])
                self.assertEqual(item["text"], [])
                prs, (lo, hi) = selection(data, [s for s in item["steps"] if s["op"] != "period"])
                shown = int(item["snap"]["kpis"]["Merged PRs"]["value"].replace(",", ""))
                self.assertEqual(shown, len(prs), "KPI merged PR count")
                # size table n
                with_lines = [p for p in prs if p["lines"] is not None]
                size = item["snap"]["size"]
                if prs:
                    self.assertEqual(int(size[0][1].replace(",", "")), len(with_lines))
                    by_type = item["snap"]["byType"]
                    self.assertEqual(sum(int(r[1].replace(",", "")) for r in by_type), len(prs), "type table sums to merged count")
                else:
                    self.assertTrue(all(c == "–" for c in size[0][1:]))
                # day-granularity chart table sums to the same number
                dt_ = item["dayTable"]
                if prs or dt_ is not None:
                    self.assertIsNotNone(dt_, "day table")
                    self.assertEqual(sum(int(float(r[1].replace(",", ""))) for r in dt_["rows"]), len(prs), "sum of daily bars")

    def test_d4_filters_actually_change_numbers(self):
        by = {(i["ds"], i["name"]): int(i["snap"]["kpis"]["Merged PRs"]["value"].replace(",", "")) for i in self.R["filters"]}
        for ds in ("merge", "api"):
            self.assertGreater(by[(ds, "all")], by[(ds, "12m")])
            self.assertGreater(by[(ds, "12m")], by[(ds, "3m")])
            self.assertGreater(by[(ds, "all")], by[(ds, "one-type-off")])
            self.assertEqual(by[(ds, "all-types-off")], 0)
            self.assertEqual(by[(ds, "custom-before-data")], 0)
            self.assertEqual(by[(ds, "period-month")], by[(ds, "all")])
        self.assertGreater(by[("api", "all")], by[("api", "bots-off")])

    def test_d4_zero_selection_renders_no_data_states(self):
        for item in self.R["filters"]:
            if item["name"] in ("all-types-off", "custom-before-data", "custom-inverted"):
                with self.subTest(item["ds"], scenario=item["name"]):
                    self.assertTrue(any("No data" in e for e in item["snap"]["empties"]), item["snap"]["empties"][:3])
                    self.assertEqual(item["snap"]["kpis"]["Merged PRs"]["value"], "0")

    # ------------------------------------------------------------------ D5
    def _check_stat_row(self, cells, vals, fmt="num", label=""):
        want = stat_row(vals)
        if want is None:
            self.assertTrue(all(c == "–" for c in cells[1:]), (label, cells))
            return
        self.assertEqual(int(cells[0].replace(",", "")), want["n"], f"{label} n")
        for key, cell in zip(COLS[1:], cells[1:]):
            if fmt == "num":
                got = parse_num(cell)
                self.assertIsNotNone(got, (label, key, cell))
                self.assertAlmostEqual(got, want[key], delta=0.051 + 1e-9, msg=f"{label} {key}: shown {cell}, expected {want[key]}")
            else:
                got, unit = parse_dur(cell)
                self.assertIsNotNone(got, (label, key, cell))
                self.assertAlmostEqual(got, want[key], delta=0.051 * unit + 1.01, msg=f"{label} {key}: shown {cell}, expected {want[key]}")

    def test_d5_size_statistics_match_python(self):
        for name in ("merge", "squash", "api"):
            data = self.sets[name]["data"]
            merged = [p for p in data["prs"] if p["state"] == "merged"]
            rows = {r[0]: r[1:] for r in self.R["stats"][name]["snap"]["size"]}
            for label, key in (("Lines changed (added + deleted)", "lines"), ("Lines added", "additions"), ("Lines deleted", "deletions"),
                               ("Files changed", "files"), ("Commits per PR", "commits")):
                with self.subTest(name, metric=key):
                    self._check_stat_row(rows[label][:10], [p[key] for p in merged], "num", f"{name}.{key}")

    def test_d5_duration_statistics_match_python(self):
        keys = {"Coding time": "coding", "Pickup time": "pickup", "Review time": "review", "Open to merge": "open_to_merge",
                "Lead time": "lead", "Merge to release": "deploy_wait", "Lead time to release": "lead_to_deploy"}
        for name in ("merge", "api"):
            data = self.sets[name]["data"]
            merged = [p for p in data["prs"] if p["state"] == "merged"]
            for row in self.R["stats"][name]["snap"]["duration"]:
                key = keys[row[0]]
                with self.subTest(name, metric=key):
                    self._check_stat_row(row[1:11], [p[key] for p in merged], "dur", f"{name}.{key}")

    def test_d5_prs_per_period_statistics_match_python(self):
        for name in ("merge", "squash", "api"):
            data = self.sets[name]["data"]
            lo, hi = js_range(data, [])
            times = [p["merged"] for p in data["prs"] if p["state"] == "merged" and lo <= p["merged"] <= hi]
            rows = {r[0]: r[1:] for r in self.R["stats"][name]["snap"]["periodStats"]}
            for kind in ("day", "week", "month"):
                with self.subTest(name, kind=kind):
                    self._check_stat_row(rows[f"per {kind}"][:10], trimmed_period_counts(times, kind, lo, hi), "num", f"{name}.per {kind}")

    def test_d5_risk_score_statistics_match_python(self):
        for name in ("merge", "api"):
            merged = [p for p in self.sets[name]["data"]["prs"] if p["state"] == "merged"]
            row = self.R["stats"][name]["riskStats"][0]["rows"][0]
            self._check_stat_row(row[1:11], [p["risk"] for p in merged], "num", f"{name}.risk")

    def test_d5_kpis_match_python(self):
        for name in ("merge", "squash", "api"):
            data = self.sets[name]["data"]
            merged = [p for p in data["prs"] if p["state"] == "merged"]
            k = self.R["stats"][name]["snap"]["kpis"]
            lines_ = sorted(p["lines"] for p in merged if p["lines"] is not None)
            self.assertEqual(int(k["Median PR size"]["value"].replace(",", "")), math.floor(percentile(lines_, 50) + 0.5), name)
            cfr = sum(1 for p in merged if p["kind"]) / len(merged)
            self.assertAlmostEqual(float(re.sub(r"(Elite|High|Medium|Low)$", "", k["Change failure rate"]["value"]).rstrip("%")), cfr * 100, delta=0.051)
            hi_risk = sum(1 for p in merged if p["risk_level"] in ("high", "critical")) / len(merged)
            self.assertAlmostEqual(float(k["High or critical risk"]["value"].rstrip("%")), hi_risk * 100, delta=0.051)
            self.assertEqual(int(re.sub(r"(Elite|High|Medium|Low)$", "", k["Releases"]["value"]).replace(",", "")), len(data["deploys"]))

    def test_d5_size_bucket_table_matches_python(self):
        data = self.sets["merge"]["data"]
        merged = [p for p in data["prs"] if p["state"] == "merged"]
        buckets = {"XS": 0, "S": 0, "M": 0, "L": 0, "XL": 0}
        for p in merged:
            v = p["lines"]
            buckets["XS" if v <= 10 else "S" if v <= 100 else "M" if v <= 500 else "L" if v <= 1000 else "XL"] += 1
        rows = {r[0]: int(r[1].replace(",", "")) for r in self.R["stats"]["merge"]["sizeBuckets"][0]["rows"]}
        self.assertEqual(rows, buckets)

    def test_d5_risk_level_table_matches_python(self):
        merged = [p for p in self.sets["merge"]["data"]["prs"] if p["state"] == "merged"]
        want = {lv: sum(1 for p in merged if p["risk_level"] == lv) for lv in ("low", "medium", "high", "critical")}
        rows = {r[0]: int(r[1].replace(",", "")) for r in self.R["stats"]["merge"]["byRisk"][0]["rows"]}
        self.assertEqual(rows, want)

    def test_d5_type_chart_table_matches_python(self):
        merged = [p for p in self.sets["merge"]["data"]["prs"] if p["state"] == "merged"]
        want = {g: sum(1 for p in merged if group_of(p["type"]) == g) for g in TYPE_GROUPS}
        rows = self.R["stats"]["merge"]["byTypeChart"][0]["rows"]
        got = {}
        for r in rows:
            label = r[0].split(" ")[0]
            got[label] = int(r[1].replace(",", ""))
        self.assertEqual(got, want)

    def test_d5_cdf_percentile_table(self):
        merged = [p for p in self.sets["merge"]["data"]["prs"] if p["state"] == "merged"]
        lines_ = [p["lines"] for p in merged]
        rows = {r[0]: r[1] for r in self.R["stats"]["merge"]["tCdf"][0]["rows"]}
        for pct in (50, 75, 90, 95, 99):
            want = percentile(lines_, pct)
            shown = rows[f"p{pct}"]
            got = float(shown.replace(",", "").replace("k", "e3").replace("M", "e6"))
            self.assertAlmostEqual(got, want, delta=max(0.06, want * 0.001 + 0.06) if "k" not in shown and "M" not in shown else want * 0.01, msg=f"p{pct} {shown} vs {want}")

    def test_d5_histogram_counts_sum_to_pr_count(self):
        merged = [p for p in self.sets["merge"]["data"]["prs"] if p["state"] == "merged"]
        rows = self.R["stats"]["merge"]["tHist"][0]["rows"]
        self.assertEqual(sum(int(r[1].replace(",", "")) for r in rows), len(merged))

    def test_all_time_view_counts_release_tagged_after_last_merge(self):
        data = self.sets["latetag"]["data"]
        self.assertEqual(len(data["deploys"]), 1)
        k = self.R["stats"]["latetag"]["snap"]["kpis"]
        self.assertEqual(re.sub(r"(Elite|High|Medium|Low)$", "", k["Releases"]["value"]), "1", "release tag dated after the newest PR/commit is dropped from All time")

    def test_kpi_and_dora_median_lead_time_agree(self):
        st = self.R["stats"]["mixedlead"]
        kpi = st["snap"]["kpis"]["Median lead time"]["value"]
        dora = st["lead"][0]["rows"][0]
        median_cell = dora[3]  # Metric, n, mean, median
        kpi_clean = re.sub(r"(Elite|High|Medium|Low)$", "", kpi).strip()
        self.assertEqual(kpi_clean, median_cell, "KPI uses lead_to_deploy ?? lead per PR, DORA card uses one metric for all PRs")

    # ------------------------------------------------------------------ D6
    def test_d6_tooltips(self):
        seen = 0
        for t in self.R["tooltips"]:
            with self.subTest(t["title"], width=t["width"], where=t["where"]):
                self.assertFalse(t.get("missing"), "chart/hit area not found")
                self.assertNotIn("exception", t)
                tip = t["tip"]
                self.assertEqual(tip["display"], "block", "tooltip did not appear")
                self.assertTrue(tip["text"].strip())
                self.assertNotRegex(tip["text"], r"NaN|undefined|null|Infinity")
                self.assertGreaterEqual(tip["left"], 0)
                self.assertGreaterEqual(tip["top"], 0)
                self.assertLessEqual(tip["right"], tip["vw"] + 0.5)
                self.assertLessEqual(tip["bottom"], tip["vh"] + 0.5)
                self.assertEqual(t["after"], "none", "tooltip stays after mouse leaves")
                seen += 1
        self.assertGreaterEqual(seen, 20)
        self.assertEqual(self.R["tooltipErrors"], [])

    # ------------------------------------------------------------------ D7
    def test_d7_table_toggles(self):
        tb = self.R["tables"]
        self.assertGreaterEqual(len(tb["titles"]), 15)
        for it in tb["items"]:
            with self.subTest(it["title"]):
                self.assertEqual(it["before"]["pressed"], "false")
                self.assertTrue(it["on"]["chartHidden"])
                self.assertFalse(it["on"]["tableHidden"])
                self.assertEqual(it["on"]["pressed"], "true")
                self.assertGreater(it["on"]["rows"], 0)
                self.assertNotRegex(it["on"]["text"], r"\bNaN\b|undefined|\bnull\b")
                self.assertFalse(it["off"]["chartHidden"])
                self.assertTrue(it["off"]["tableHidden"])
                self.assertEqual(it["off"]["pressed"], "false")
        self.assertEqual(tb["errors"], [])

    # ------------------------------------------------------------------ D8
    def test_d8_empty_and_single_pr(self):
        rows = [r for r in self.R["load"] if r["ds"] in ("empty", "single")]
        self.assertTrue(rows)
        for r in rows:
            with self.subTest(r["ds"], theme=r["theme"], width=r["width"]):
                self.assertEqual(r["errors"] + r["pageErrors"], [])
                self.assertEqual(r["text"], [])
        by = {c["ds"]: c for c in self.R["controls"]}
        self.assertEqual(by["empty"]["issues"], [])
        self.assertEqual(by["single"]["issues"], [])

    def test_d8_empty_repo_message(self):
        b = self.R["banners"]["empty"]
        self.assertIn("No pull requests", self.R["banners"]["empty"]["banner"] + " ".join(b["li"]))

    # ------------------------------------------------------------------ D9
    def test_d9_git_banner_inferred_and_api_banner_exact(self):
        b = self.R["banners"]
        for name in ("merge", "squash", "notags", "single", "empty"):
            self.assertIn("inferred from git history", b[name]["banner"], name)
            self.assertFalse(b[name]["isOk"], name)
        self.assertIn("Exact PR data", b["api"]["banner"])
        self.assertTrue(b["api"]["isOk"])
        self.assertNotIn("inferred", b["api"]["banner"].split("\n")[0])

    def test_d9_warnings_listed(self):
        b = self.R["banners"]
        for name in ("notags", "empty", "merge", "squash"):
            self.assertEqual(b[name]["li"], self.sets[name]["data"]["meta"]["warnings"], name)
        self.assertTrue(any("release tags" in w for w in b["notags"]["li"]))

    def test_d9_needs_api_labels(self):
        b = self.R["banners"]
        for name in ("merge", "squash"):
            src = {r[0]: r[1] for r in b[name]["needsApi"]}
            self.assertEqual(src["Pickup time"], "needs API", name)
            self.assertEqual(src["Open to merge"], "needs API", name)
        src = {r[0]: r[1] for r in b["api"]["needsApi"]}
        self.assertEqual(src["Pickup time"], "exact")
        self.assertEqual(src["Open to merge"], "exact")
        src = {r[0]: r[1] for r in b["merge"]["needsApi"]}
        self.assertIn("inferred", src["Lead time"])
        self.assertIn("inferred", src["Coding time"])
        self.assertIn("inferred", src["Lead time to release"], "lead-to-release is built from the inferred first-commit time")

    # ------------------------------------------------------------------ D10
    def test_d10_keyboard_and_aria(self):
        a = self.R["a11y"]
        seq = a["seq"]
        ids = [s["id"] for s in seq if s]
        self.assertIn("preset", ids)
        labels = [s["text"] for s in seq if s and s["tag"] == "BUTTON"]
        for want in ("feat", "Day", "Week", "Month"):
            self.assertIn(want, labels)
        self.assertTrue(any("theme" in l.lower() for l in labels), "theme button not reachable by Tab")
        self.assertEqual(a["buttonsWithoutPressed"], [])
        self.assertEqual(a["svgs"], [], "every chart svg needs role=img and an aria-label")
        self.assertGreater(a["svgCount"], 12)
        self.assertEqual(a["selectsUnlabelled"], 0)
        self.assertEqual(a["dayPressedAfterEnter"], "true")
        self.assertNotEqual(a["outline"], "none")
        self.assertEqual(a["lang"], "en")
        self.assertEqual(a["h1"], 1)

    def test_d10_chart_hit_areas_are_keyboard_operable(self):
        """Line charts expose a tabindex=0 hit rect but have no key handler, so a keyboard user can focus an
        element that does nothing and cannot read the values. (Table toggle is the fallback.)"""
        self.assertEqual(self.R["a11y"]["focusableRects"], 0)

    # ------------------------------------------------------------------ D11
    def test_d11_screenshots_written(self):
        self.assertEqual(len(self.R["shots"]), 5 * 2 * 2)
        for f in self.R["shots"]:
            self.assertGreater(os.path.getsize(f), 20_000, f)

    # ------------------------------------------------------------------ misc
    def test_resize_rerenders_without_scroll(self):
        r = self.R["resize"]
        self.assertLess(r["w1"], r["w0"])
        self.assertLessEqual(r["hscroll"], 0)
        self.assertEqual(r["errors"], [])

    def test_c1_xss_payloads_are_inert_in_browser(self):
        for name, r in self.R["xss"].items():
            with self.subTest(name):
                self.assertEqual(r["dialogs"], [])
                self.assertEqual(r["errors"], [])
                self.assertEqual(r["injected"], 0)
                self.assertEqual(r["scripts"], 1)
                self.assertFalse(r["pwned"])
                if name == "evil":
                    self.assertTrue(r["bodyHasLiteral"], "the payload should be displayed as text")
        self.assertEqual(self.R["xss"]["evil"]["h1"], EVIL)
        self.assertIn(EVIL, self.R["xss"]["evil"]["titleTag"])


if __name__ == "__main__":
    unittest.main()
