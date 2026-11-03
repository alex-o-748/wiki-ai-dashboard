#!/usr/bin/env python3
"""Collect public AI-related counters from Wikipedias and write them to Commons.

Reads change-tag hit counts, article-search totals and bot edit counts, as listed in
config.json, and writes three tabular data pages on Commons:

  Latest.tab   this run: one row per metric x wiki, plus total / wikis_nonzero / wikis_checked
  History.tab  every run, same format, appended (a re-run on the same date replaces that date)
  Totals.tab   one row per run date, one column per metric total

With --dry-run nothing is edited; the three pages are written to out/ instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("ai-metrics")

HERE = Path(__file__).resolve().parent
COMMONS = "commons.wikimedia.org"
SUMMARY_KEYS = ("total", "wikis_nonzero", "wikis_checked")

# A wiki page holds at most 2 MiB ($wgMaxArticleSize on Wikimedia wikis).
PAGE_LIMIT = 2 * 1024 * 1024
WARN_AT = 0.85
# Refuse to publish if more than this share of values failed: better last week's
# numbers than a page of blanks after an outage.
MAX_FAILED_SHARE = 0.25

LICENSE = "CC0-1.0"
SOURCES = ("Collected weekly by https://github.com/alex-o-748/wiki-ai-dashboard from each Wikipedia's "
           "Action API: list=tags (change-tag hit counts, as on Special:Tags), list=search "
           "(article search totals) and list=users (edit counts).")

LONG_FIELDS = [
    {"name": "date", "type": "string", "title": {"en": "Date of the run (UTC)"}},
    {"name": "metric", "type": "string", "title": {"en": "Metric"}},
    {"name": "wiki", "type": "string",
     "title": {"en": "Wiki language code, or total / wikis_nonzero / wikis_checked"}},
    {"name": "value", "type": "number", "title": {"en": "Value"}},
]

Values = Dict[str, Optional[int]]  # wiki -> value, None when the wiki could not be read


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------- API client

class Api:
    """A polite MediaWiki Action API client: one request at a time, a pause between
    requests, maxlag, and retries that honour Retry-After."""

    RETRY_STATUS = {429, 500, 502, 503, 504}
    RETRY_CODES = {"maxlag", "ratelimited", "readonly", "internal_api_error_DBConnectionError",
                   "internal_api_error_DBQueryError", "internal_api_error_DBQueryTimeoutError"}
    BUSY_WORDS = ("too busy", "too many", "try again", "timed out", "timeout")

    def __init__(self, user_agent: str, delay: float = 1.0, tries: int = 6, maxlag: int = 5):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"{user_agent} python-requests/{requests.__version__}"
        self.delay = delay
        self.tries = tries
        self.maxlag = maxlag
        self.count = 0
        self._last = 0.0

    def call(self, host: str, params: Dict[str, Any], post: bool = False) -> Dict[str, Any]:
        params = dict(params, format="json", formatversion="2", maxlag=self.maxlag)
        url = f"https://{host}/w/api.php"
        what = f"{host} {params.get('action')}/{params.get('list') or params.get('meta') or params.get('prop') or ''}"
        for attempt in range(1, self.tries + 1):
            wait = min(5 * 2 ** (attempt - 1), 300)
            self._pace()
            try:
                if post:
                    resp = self.session.post(url, data=params, timeout=90)
                else:
                    resp = self.session.get(url, params=params, timeout=90)
            except requests.RequestException as exc:
                reason = f"network error: {exc.__class__.__name__}"
            else:
                self.count += 1
                if resp.status_code in self.RETRY_STATUS:
                    reason = f"HTTP {resp.status_code}"
                    wait = max(wait, retry_after(resp))
                else:
                    resp.raise_for_status()
                    data = resp.json()
                    error = data.get("error")
                    if not error:
                        return data
                    code, info = error.get("code", ""), error.get("info", "")
                    if code in self.RETRY_CODES or any(w in info.lower() for w in self.BUSY_WORDS):
                        reason = f"{code}: {info}"
                        wait = max(wait, retry_after(resp))
                    else:
                        raise ApiError(f"{what}: {code}: {info}")
            if attempt < self.tries:
                wait = min(wait, 900)
                log.info("%s: %s; retrying in %ds (%d/%d)", what, reason, wait, attempt, self.tries - 1)
                time.sleep(wait)
        raise ApiError(f"{what}: gave up after {self.tries} tries ({reason})")

    def _pace(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()


def retry_after(resp: requests.Response) -> int:
    """Seconds asked for by a Retry-After header (seconds or HTTP date), or 0."""
    value = resp.headers.get("Retry-After", "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0, int((when - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def wiki_host(wiki: str) -> str:
    return f"{wiki}.wikipedia.org"


# --------------------------------------------------------------------------- readers

def fetch_tag_counts(api: Api, wiki: str) -> Dict[str, int]:
    """Every change tag on a wiki with its hit count, as Special:Tags shows it."""
    counts: Dict[str, int] = {}
    cont: Dict[str, str] = {}
    while True:
        data = api.call(wiki_host(wiki), {"action": "query", "list": "tags", "tglimit": "max",
                                          "tgprop": "hitcount", **cont})
        for tag in data["query"]["tags"]:
            counts[tag["name"]] = int(tag.get("hitcount", 0))
        if "continue" not in data:
            return counts
        cont = data["continue"]


def fetch_search_total(api: Api, wiki: str, query: str) -> int:
    """Number of articles (namespace 0) matching a search."""
    for attempt in (1, 2, 3):
        data = api.call(wiki_host(wiki), {"action": "query", "list": "search", "srsearch": query,
                                          "srnamespace": 0, "srlimit": 1, "srinfo": "totalhits",
                                          "srprop": ""})
        warning = json.dumps(data.get("warnings", {})).lower()
        if "timed out" in warning or "timeout" in warning:
            # A timed-out search reports a partial count. Don't record an undercount.
            log.info("%s: search for %s timed out; retrying in 30s", wiki, query)
            time.sleep(30)
            continue
        return int(data["query"]["searchinfo"]["totalhits"])
    raise ApiError(f"{wiki}: search for {query} timed out 3 times")


def normalise_user(name: str) -> str:
    name = name.replace("_", " ").strip()
    return name[:1].upper() + name[1:]


def fetch_edit_counts(api: Api, wiki: str, users: List[str]) -> Dict[str, Optional[int]]:
    """Edit counts for a few accounts on one wiki; None for an account that doesn't exist."""
    data = api.call(wiki_host(wiki), {"action": "query", "list": "users", "ususers": "|".join(users),
                                      "usprop": "editcount"})
    found = {normalise_user(u["name"]): u for u in data["query"]["users"]}
    out: Dict[str, Optional[int]] = {}
    for user in users:
        info = found.get(normalise_user(user))
        if info is None or info.get("missing") or info.get("invalid") or "editcount" not in info:
            out[user] = None
        else:
            out[user] = int(info["editcount"])
    return out


# --------------------------------------------------------------------------- collection

def metric_wikis(config: Dict[str, Any], spec: Dict[str, Any]) -> List[str]:
    if spec["source"] == "editcount":
        return list(spec["users"])
    wikis = spec["wikis"]
    return list(config["wiki_groups"][wikis]) if isinstance(wikis, str) else list(wikis)


def collect(api: Api, config: Dict[str, Any], only: Optional[List[str]] = None,
            skip_search: bool = False) -> Dict[str, Values]:
    """Read every metric in config. A value is None when its wiki could not be read."""
    metrics = {name: spec for name, spec in config["metrics"].items()
               if not (skip_search and spec["source"] == "search")}

    def wanted(wiki: str) -> bool:
        return only is None or wiki in only

    results: Dict[str, Values] = {name: {} for name in metrics}

    # Change tags: one listing per wiki serves every tag metric.
    tag_cache: Dict[str, Any] = {}
    for name, spec in metrics.items():
        if spec["source"] != "tag":
            continue
        for wiki in filter(wanted, metric_wikis(config, spec)):
            if wiki not in tag_cache:
                try:
                    tag_cache[wiki] = fetch_tag_counts(api, wiki)
                except (ApiError, requests.RequestException, ValueError, KeyError) as exc:
                    log.warning("%s: could not read change tags, leaving its values empty: %s", wiki, exc)
                    tag_cache[wiki] = None
            counts = tag_cache[wiki]
            if counts is None:
                results[name][wiki] = None
                continue
            if "tags_by_wiki" in spec:
                tags = spec["tags_by_wiki"].get(wiki, [])
                specific = True
            else:
                tags = spec["tags"]
                specific = False
            for tag in tags:
                if tag not in counts:
                    # A wiki-specific tag that vanished was probably renamed: say so loudly.
                    (log.warning if specific else log.info)(
                        "%s: tag %r not defined on %s, counting it as 0", name, tag, wiki)
            results[name][wiki] = sum(counts.get(tag, 0) for tag in tags)

    # Edit counts: one request per wiki for all the accounts on it.
    by_wiki: Dict[str, List[tuple]] = {}
    for name, spec in metrics.items():
        if spec["source"] == "editcount":
            for wiki, user in spec["users"].items():
                if wanted(wiki):
                    by_wiki.setdefault(wiki, []).append((name, user))
    for wiki, pairs in by_wiki.items():
        try:
            counts = fetch_edit_counts(api, wiki, [user for _, user in pairs])
        except (ApiError, requests.RequestException, ValueError, KeyError) as exc:
            log.warning("%s: could not read edit counts, leaving them empty: %s", wiki, exc)
            counts = {}
        for name, user in pairs:
            value = counts.get(user)
            if value is None and counts:
                log.warning("%s: account %r not found on %s, leaving it empty", name, user, wiki)
            results[name][wiki] = value

    # Searches.
    for name, spec in metrics.items():
        if spec["source"] != "search":
            continue
        for wiki in filter(wanted, metric_wikis(config, spec)):
            try:
                results[name][wiki] = fetch_search_total(api, wiki, spec["query"])
            except (ApiError, requests.RequestException, ValueError, KeyError) as exc:
                log.warning("%s: search failed on %s, leaving it empty: %s", name, wiki, exc)
                results[name][wiki] = None

    return results


def summarise(values: Values) -> Dict[str, Optional[int]]:
    got = [v for v in values.values() if v is not None]
    return {
        "total": sum(got) if got else None,
        "wikis_nonzero": sum(1 for v in got if v > 0),
        "wikis_checked": len(got),
    }


# --------------------------------------------------------------------------- pages

def long_rows(date: str, results: Dict[str, Values]) -> List[list]:
    rows = []
    for metric, values in results.items():
        for wiki, value in values.items():
            rows.append([date, metric, wiki, value])
        for key, value in summarise(values).items():
            rows.append([date, metric, key, value])
    return rows


def tab_page(description: str, fields: List[dict], rows: List[list]) -> Dict[str, Any]:
    return {
        "license": LICENSE,
        "description": {"en": description},
        "sources": SOURCES,
        "mediawikiCategories": [{"name": "Wikimedia-related tabular data", "sort": "AI"}],
        "schema": {"fields": fields},
        "data": rows,
    }


def latest_page(date: str, results: Dict[str, Values]) -> Dict[str, Any]:
    return tab_page(
        "AI-related counters on Wikipedias, latest weekly run. One row per metric and wiki, plus "
        "total, wikis_nonzero and wikis_checked per metric. An empty value means the wiki could not "
        "be read. Read by meta:Module:AI dashboard.",
        LONG_FIELDS, long_rows(date, results))


def history_page(date: str, results: Dict[str, Values], existing: Optional[dict]) -> Dict[str, Any]:
    rows: List[list] = []
    if existing:
        names = [f["name"] for f in existing["schema"]["fields"]]
        if names != [f["name"] for f in LONG_FIELDS]:
            raise ValueError(f"History.tab has columns {names}, expected {[f['name'] for f in LONG_FIELDS]}")
        rows = [row for row in existing["data"] if row[0] != date]
    rows.extend(long_rows(date, results))
    return tab_page(
        "AI-related counters on Wikipedias, every weekly run, in the same format as Latest.tab.",
        LONG_FIELDS, rows)


def totals_page(date: str, results: Dict[str, Values], existing: Optional[dict]) -> Dict[str, Any]:
    """One row per date, one column per metric total. Columns are never dropped: a metric
    removed from config keeps its column (empty from then on), a new one adds a column."""
    columns: List[str] = []
    fields: List[dict] = [{"name": "date", "type": "string", "title": {"en": "Date of the run (UTC)"}}]
    old_rows: List[list] = []
    if existing:
        old_fields = existing["schema"]["fields"]
        if not old_fields or old_fields[0]["name"] != "date":
            raise ValueError("Totals.tab must start with a date column")
        fields = list(old_fields)
        columns = [f["name"] for f in old_fields[1:]]
        old_rows = [row for row in existing["data"] if row[0] != date]
    for metric in results:
        if metric not in columns:
            columns.append(metric)
            fields.append({"name": metric, "type": "number", "title": {"en": metric}})
    width = len(fields)
    rows = [list(row) + [None] * (width - len(row)) for row in old_rows]
    totals = {metric: summarise(values)["total"] for metric, values in results.items()}
    rows.append([date] + [totals.get(col) for col in columns])
    rows.sort(key=lambda row: row[0])
    return tab_page(
        "Totals of AI-related counters on Wikipedias, one row per weekly run, one column per "
        "metric (see Latest.tab for what each counts and on which wikis).",
        fields, rows)


def page_text(page: Dict[str, Any]) -> str:
    """Compact JSON with one data row per line, so diffs on Commons stay readable.
    Commons stores tabular data as sent, so this is also what counts toward the size limit."""
    head = {k: v for k, v in page.items() if k != "data"}
    rows = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in page["data"]]
    head_text = json.dumps(head, ensure_ascii=False, separators=(",", ":"))
    return head_text[:-1] + ',"data":[\n' + ",\n".join(rows) + "\n]}\n"


# --------------------------------------------------------------------------- Commons

class Commons:
    def __init__(self, api: Api):
        self.api = api
        self.csrf: Optional[str] = None

    def read(self, title: str) -> tuple:
        """(parsed JSON, revision) of a page, or (None, None) if it doesn't exist."""
        data = self.api.call(COMMONS, {"action": "query", "prop": "revisions", "titles": title,
                                       "rvprop": "content|ids|timestamp", "rvslots": "main"})
        page = data["query"]["pages"][0]
        if page.get("missing"):
            return None, None
        rev = page["revisions"][0]
        return json.loads(rev["slots"]["main"]["content"]), rev

    def login(self, user: str, password: str) -> None:
        token = self.api.call(COMMONS, {"action": "query", "meta": "tokens",
                                        "type": "login"})["query"]["tokens"]["logintoken"]
        result = self.api.call(COMMONS, {"action": "login", "lgname": user, "lgpassword": password,
                                         "lgtoken": token}, post=True)["login"]
        if result.get("result") != "Success":
            raise ApiError(f"login as {user} failed: {result.get('reason') or result.get('result')}")
        self.csrf = self.api.call(COMMONS, {"action": "query", "meta": "tokens"})["query"]["tokens"]["csrftoken"]
        log.info("logged in to Commons as %s", result.get("lgusername", user))

    def save(self, title: str, text: str, summary: str, base: Optional[dict]) -> None:
        """Save a page read earlier with read(); base is the revision it returned."""
        params = {"action": "edit", "title": title, "text": text, "summary": summary,
                  "bot": 1, "assert": "user", "token": self.csrf}
        if base:
            # Fail rather than overwrite if someone edited the page since we read it.
            params.update(baserevid=base["revid"], basetimestamp=base["timestamp"])
        else:
            # It didn't exist when we read it: fail if someone created it meanwhile.
            params["createonly"] = 1
        result = self.api.call(COMMONS, params, post=True)["edit"]
        if result.get("result") != "Success":
            raise ApiError(f"saving {title} failed: {result}")
        log.info("saved %s%s", title, " (no change)" if result.get("nochange") else
                 f" (revision {result.get('newrevid')})")


# --------------------------------------------------------------------------- main

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="write the pages to --out instead of Commons")
    p.add_argument("--only", help="comma-separated wiki codes to read, e.g. en,pl (needs --dry-run)")
    p.add_argument("--skip-search", action="store_true", help="skip the article searches (needs --dry-run)")
    p.add_argument("--config", default=str(HERE / "config.json"))
    p.add_argument("--out", default=str(HERE / "out"), help="where --dry-run writes the pages")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    args = p.parse_args(argv)
    if (args.only or args.skip_search) and not args.dry_run:
        p.error("--only and --skip-search make a partial run, which must not overwrite the real "
                "totals: add --dry-run")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    only = [w.strip() for w in args.only.split(",")] if args.only else None

    user = os.environ.get("AI_METRICS_BOT_USER")
    password = os.environ.get("AI_METRICS_BOT_PASSWORD")
    if not args.dry_run and not (user and password):
        log.error("set AI_METRICS_BOT_USER and AI_METRICS_BOT_PASSWORD, or use --dry-run")
        return 2

    api = Api(config["user_agent"], delay=args.delay)
    commons = Commons(api)
    started = time.monotonic()

    results = collect(api, config, only=only, skip_search=args.skip_search)

    n_values = sum(len(v) for v in results.values())
    n_failed = sum(1 for values in results.values() for v in values.values() if v is None)

    # Read the current pages: History and Totals are appended to, and every save checks that
    # nobody edited the page in between. A real run stops if it can't read them, rather
    # than start the history afresh.
    pages = config["pages"]
    current: Dict[str, tuple] = {}
    try:
        for name in ("latest", "history", "totals"):
            current[name] = commons.read(pages[name])
    except (ApiError, requests.RequestException, ValueError, KeyError) as exc:
        if not args.dry_run:
            log.error("could not read the existing pages from Commons, not saving anything: %s", exc)
            return 1
        log.warning("could not read the existing pages from Commons, starting them afresh: %s", exc)
        current = {name: (None, None) for name in ("latest", "history", "totals")}

    try:
        built = {
            "latest": latest_page(date, results),
            "history": history_page(date, results, current["history"][0]),
            "totals": totals_page(date, results, current["totals"][0]),
        }
    except ValueError as exc:
        log.error("the page on Commons isn't in the expected format, not saving anything: %s", exc)
        return 1
    texts = {name: page_text(page) for name, page in built.items()}

    for name, text in texts.items():
        size = len(text.encode("utf-8"))
        if size > PAGE_LIMIT:
            log.error("%s would be %d KB, over the %d KB page limit: archive its older rows "
                      "(see README)", pages[name], size // 1024, PAGE_LIMIT // 1024)
            return 1
        if size > WARN_AT * PAGE_LIMIT:
            log.warning("%s is %d KB of %d KB: archive its older rows soon (see README)",
                        pages[name], size // 1024, PAGE_LIMIT // 1024)

    width = max(len(m) for m in results)
    for metric, values in results.items():
        s = summarise(values)
        total = "–" if s["total"] is None else s["total"]
        failed = len(values) - s["wikis_checked"]
        log.info("%-*s total=%s nonzero=%d/%d%s", width, metric, total, s["wikis_nonzero"],
                 s["wikis_checked"], f" failed={failed}" if failed else "")

    if args.dry_run:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, text in texts.items():
            path = out / pages[name].split("/")[-1]
            path.write_text(text, encoding="utf-8")
            log.info("wrote %s (%d KB)", path, len(text.encode("utf-8")) // 1024)
        log.info("dry run: %d values (%d failed), %d requests, %.0fs",
                 n_values, n_failed, api.count, time.monotonic() - started)
        return 0

    if n_values == 0 or n_failed > MAX_FAILED_SHARE * n_values:
        log.error("%d of %d values failed, more than %d%%: not saving, the pages keep last week's numbers",
                  n_failed, n_values, MAX_FAILED_SHARE * 100)
        return 1

    summary = (f"Weekly update of AI metrics for {date}: {n_values - n_failed} values"
               + (f", {n_failed} could not be read" if n_failed else "")
               + " (https://github.com/alex-o-748/wiki-ai-dashboard)")
    try:
        commons.login(user, password)
        for name in ("history", "totals", "latest"):
            commons.save(pages[name], texts[name], summary, current[name][1])
    except (ApiError, requests.RequestException, ValueError, KeyError) as exc:
        log.error("saving to Commons failed: %s", exc)
        return 1
    log.info("done: %d values (%d failed), %d requests, %.0fs",
             n_values, n_failed, api.count, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
