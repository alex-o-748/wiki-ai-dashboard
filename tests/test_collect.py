"""Offline tests for collect.py: no network. Run with  python3 -m unittest discover tests"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collect  # noqa: E402

CONFIG = json.loads((Path(collect.HERE) / "config.json").read_text(encoding="utf-8"))


class FakeApi:
    """Answers the three kinds of read the collector makes, from canned data."""

    def __init__(self, tags=None, users=None, search=None, fail=()):
        self.tags = tags or {}        # wiki -> {tag: hitcount}
        self.users = users or {}      # wiki -> {user: editcount}
        self.search = search or {}    # (wiki, query) -> totalhits
        self.fail = set(fail)         # wikis whose every request fails
        self.calls = []

    def call(self, host, params, post=False):
        wiki = host.split(".")[0]
        self.calls.append((wiki, params.get("list")))
        if wiki in self.fail:
            raise collect.ApiError(f"{host}: gave up")
        if params["list"] == "tags":
            tags = [{"name": k, "hitcount": v} for k, v in self.tags.get(wiki, {}).items()]
            if params.get("tgcontinue"):
                return {"query": {"tags": tags[1:]}}
            if len(tags) > 1:  # serve the listing in two pages, as a big wiki would
                return {"query": {"tags": tags[:1]}, "continue": {"tgcontinue": "x", "continue": "-||"}}
            return {"query": {"tags": tags}}
        if params["list"] == "users":
            out = []
            for name in params["ususers"].split("|"):
                n = collect.normalise_user(name)
                if n in self.users.get(wiki, {}):
                    out.append({"name": n, "editcount": self.users[wiki][n]})
                else:
                    out.append({"name": n, "missing": True})
            return {"query": {"users": out}}
        if params["list"] == "search":
            return {"query": {"searchinfo": {"totalhits": self.search.get((wiki, params["srsearch"]), 0)}}}
        raise AssertionError(params)


def small_config():
    return {
        "wiki_groups": {"tag_wikis": ["en", "pl", "de"], "search_wikis": ["en", "pl"]},
        "metrics": {
            "content_translation": {"source": "tag", "wikis": "tag_wikis", "tags": ["contenttranslation"]},
            "community_ai_filter": {"source": "tag", "wikis": "tag_wikis",
                                    "tags_by_wiki": {"en": ["a", "b"], "pl": ["gone"]}},
            "chatgpt_links": {"source": "search", "wikis": "search_wikis", "query": "insource:\"x\""},
            "bot_edits": {"source": "editcount", "users": {"en": "Some bot", "pl": "Missing bot"}},
        },
    }


class CollectTest(unittest.TestCase):
    def test_reads_and_sums(self):
        api = FakeApi(
            tags={"en": {"contenttranslation": 10, "a": 3, "b": 4}, "pl": {"contenttranslation": 5}, "de": {}},
            users={"en": {"Some bot": 99}},
            search={("en", "insource:\"x\""): 7, ("pl", "insource:\"x\""): 0},
        )
        with self.assertLogs("ai-metrics", "INFO") as logs:
            r = collect.collect(api, small_config())
        self.assertEqual(r["content_translation"], {"en": 10, "pl": 5, "de": 0})
        # Two tags on one wiki are added; a wiki without a filter counts as checked, 0.
        self.assertEqual(r["community_ai_filter"], {"en": 7, "pl": 0, "de": 0})
        self.assertEqual(r["chatgpt_links"], {"en": 7, "pl": 0})
        self.assertEqual(r["bot_edits"], {"en": 99, "pl": None})
        text = "\n".join(logs.output)
        self.assertIn("WARNING:ai-metrics:community_ai_filter: tag 'gone' not defined on pl", text)
        self.assertIn("WARNING:ai-metrics:bot_edits: account 'Missing bot' not found on pl", text)
        # One tag listing per wiki (two pages for en), however many tag metrics use it.
        self.assertEqual(sum(1 for c in api.calls if c == ("en", "tags")), 2)
        self.assertEqual(sum(1 for c in api.calls if c == ("pl", "tags")), 1)

    def test_failed_wiki_is_empty_and_excluded(self):
        api = FakeApi(tags={"en": {"contenttranslation": 10}, "de": {"contenttranslation": 1}},
                      users={"en": {"Some bot": 1}}, fail={"pl"})
        with self.assertLogs("ai-metrics", "WARNING") as logs:
            r = collect.collect(api, small_config())
        self.assertIsNone(r["content_translation"]["pl"])
        self.assertIsNone(r["chatgpt_links"]["pl"])
        self.assertEqual(collect.summarise(r["content_translation"]),
                         {"total": 11, "wikis_nonzero": 2, "wikis_checked": 2})
        self.assertTrue(any("pl: could not read change tags" in line for line in logs.output))

    def test_only_and_skip_search(self):
        api = FakeApi(tags={"en": {"contenttranslation": 1, "a": 1, "b": 1}}, users={"en": {"Some bot": 1}})
        r = collect.collect(api, small_config(), only=["en"], skip_search=True)
        self.assertNotIn("chatgpt_links", r)
        self.assertEqual(set(r["content_translation"]), {"en"})
        self.assertEqual(set(r["bot_edits"]), {"en"})
        self.assertTrue(all(wiki == "en" for wiki, _ in api.calls))

    def test_search_timeout_is_not_recorded(self):
        class TimingOut(FakeApi):
            def call(self, host, params, post=False):
                return {"warnings": {"search": {"warnings": "The search timed out"}},
                        "query": {"searchinfo": {"totalhits": 3}}}
        with mock.patch("time.sleep"):
            with self.assertRaises(collect.ApiError):
                collect.fetch_search_total(TimingOut(), "en", "q")

    def test_real_config_is_consistent(self):
        groups = CONFIG["wiki_groups"]
        self.assertEqual(len(groups["tag_wikis"]), 15)
        self.assertEqual(len(groups["search_wikis"]), 30)
        for wikis in groups.values():
            self.assertEqual(len(wikis), len(set(wikis)))
        for name, spec in CONFIG["metrics"].items():
            self.assertRegex(name, r"^[a-zA-Z_][a-zA-Z_0-9]*$")  # valid as a Totals.tab column
            self.assertIn(spec["source"], ("tag", "search", "editcount"))
            if spec["source"] != "editcount":
                wikis = collect.metric_wikis(CONFIG, spec)
                for wiki in spec.get("tags_by_wiki", {}):
                    self.assertIn(wiki, wikis, name)
        self.assertEqual(len(CONFIG["metrics"]["community_ai_filter"]["tags_by_wiki"]), 9)
        self.assertEqual(len(CONFIG["metrics"]["automoderator_edits"]["users"]), 12)


class PagesTest(unittest.TestCase):
    results = {"m1": {"en": 5, "pl": 0, "de": None}, "m2": {"en": 2}}

    def test_latest_rows(self):
        page = collect.latest_page("2026-11-09", self.results)
        self.assertEqual([f["name"] for f in page["schema"]["fields"]], ["date", "metric", "wiki", "value"])
        rows = {(r[1], r[2]): r[3] for r in page["data"]}
        self.assertEqual(rows[("m1", "en")], 5)
        self.assertIsNone(rows[("m1", "de")])
        self.assertEqual(rows[("m1", "total")], 5)
        self.assertEqual(rows[("m1", "wikis_nonzero")], 1)
        self.assertEqual(rows[("m1", "wikis_checked")], 2)
        self.assertTrue(all(r[0] == "2026-11-09" for r in page["data"]))
        self.assertEqual(page["license"], "CC0-1.0")

    def test_history_appends_and_replaces_same_date(self):
        first = collect.history_page("2026-11-02", self.results, None)
        second = collect.history_page("2026-11-09", self.results, first)
        self.assertEqual(len(second["data"]), 2 * len(first["data"]))
        rerun = collect.history_page("2026-11-09", {"m1": {"en": 6}}, second)
        dates = [r[0] for r in rerun["data"]]
        self.assertEqual(dates.count("2026-11-02"), len(first["data"]))
        self.assertEqual(dates.count("2026-11-09"), 4)  # en + three summary rows

    def test_history_refuses_unexpected_columns(self):
        bad = {"schema": {"fields": [{"name": "x"}]}, "data": []}
        with self.assertRaises(ValueError):
            collect.history_page("2026-11-09", self.results, bad)

    def test_totals_keep_old_columns_and_add_new(self):
        t1 = collect.totals_page("2026-11-02", {"old": {"en": 1}, "m1": {"en": 2}}, None)
        t2 = collect.totals_page("2026-11-09", {"m1": {"en": 3}, "new": {"en": 4, "pl": 5}}, t1)
        self.assertEqual([f["name"] for f in t2["schema"]["fields"]], ["date", "old", "m1", "new"])
        self.assertEqual(t2["data"], [["2026-11-02", 1, 2, None], ["2026-11-09", None, 3, 9]])
        t3 = collect.totals_page("2026-11-09", {"m1": {"en": 30}}, t2)  # same-date re-run replaces
        self.assertEqual(t3["data"], [["2026-11-02", 1, 2, None], ["2026-11-09", None, 30, None]])

    def test_page_text_round_trips(self):
        page = collect.history_page("2026-11-09", self.results, None)
        text = collect.page_text(page)
        self.assertEqual(json.loads(text), page)
        self.assertEqual(text.count("\n"), len(page["data"]) + 2)  # one row per line


class CliTest(unittest.TestCase):
    def test_partial_runs_need_dry_run(self):
        for argv in (["--only", "en"], ["--skip-search"]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                collect.parse_args(argv)
        self.assertTrue(collect.parse_args(["--dry-run", "--only", "en,pl", "--skip-search"]).dry_run)

    def test_real_run_needs_credentials(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("logging.basicConfig"), \
                self.assertLogs("ai-metrics", "ERROR"):
            self.assertEqual(collect.main([]), 2)


class RetryAfterTest(unittest.TestCase):
    def test_parses_seconds_and_dates(self):
        resp = mock.Mock(headers={"Retry-After": "7"})
        self.assertEqual(collect.retry_after(resp), 7)
        resp = mock.Mock(headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        self.assertEqual(collect.retry_after(resp), 0)
        resp = mock.Mock(headers={})
        self.assertEqual(collect.retry_after(resp), 0)


if __name__ == "__main__":
    unittest.main()
