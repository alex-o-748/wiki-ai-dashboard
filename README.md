# wiki-ai-dashboard

A weekly job that keeps the numbers on
[User:Alaexis/AI draft](https://meta.wikimedia.org/wiki/User:Alaexis/AI_draft) current.

It reads public counters from 34 Wikipedias and writes them to tabular data pages on Commons. The Meta
page reads those pages through a small Lua module, so the page itself never needs editing for a refresh.

```
Toolforge job (weekly)
  ├─ reads   Special:Tags hit counts (15 wikis), article searches (30 wikis), bot edit counts
  └─ writes  commons:Data:Wikimedia AI/Latest.tab    this week, one row per metric × wiki, plus totals
             commons:Data:Wikimedia AI/History.tab   every week, same format, appended
             commons:Data:Wikimedia AI/Totals.tab    one row per week, one column per total (chartable)

meta:Module:AI dashboard  ── reads Latest.tab ──>  {{#invoke:AI dashboard|value|content_translation}}
```

| File | What it is |
|---|---|
| [`collect.py`](collect.py) | the job |
| [`config.json`](config.json) | which wikis, tags, searches and bot accounts it reads |
| [`meta/Module_AI_dashboard.lua`](meta/Module_AI_dashboard.lua) | the Lua module for Meta |
| [`meta/usage.wikitext`](meta/usage.wikitext) | the draft's tiles and tables with the numbers swapped for `#invoke`s |
| [`tests/`](tests) | offline tests for both |

## What it collects

| Metric | Where it comes from | Wikis |
|---|---|---|
| `content_translation`, `add_link`, `editcheck_tone`, `editcheck_llm_paste` | change-tag hit counts (`list=tags&tgprop=hitcount`): the same number Special:Tags shows | 15 |
| `community_ai_filter` | each wiki's own AI-filter tag(s), listed per wiki in `config.json` | 9 of 15 have one |
| `ml_vandalism_tag` | ja's experimental deep-learning vandalism tag (kept apart: it isn't about AI-written text) | ja |
| `chatgpt_links`, `oaicite`, `copilot_links` | article search totals for `insource:"…"`, articles only | 30 |
| `cluebot_ng_edits`, `serobot_edits`, `patroclebot_edits`, `automoderator_edits` | bot account edit counts (`list=users&usprop=editcount`) | en, es, ro, 12 Automoderator wikis |

For every metric it also writes three summary rows: `total`, `wikis_nonzero` and `wikis_checked`.
`wikis_checked` counts the wikis that were read successfully, so a wiki that failed doesn't count.

**What a tag count means.** It counts every change carrying the tag since the tag was created, bots
included. MediaWiki keeps it as a running tally rather than recounting. It is not the 30-day window
that the RecentChanges links show.

**Some details:**
- A wiki without an AI filter counts as checked, with a value of 0. That is how the "9 / 15" tile
  works.
- Indonesian Wikipedia has two filter tags, and its value is their sum. An edit carrying both is
  counted twice.
- The Automoderator account has a different name on each wiki. The names come from the deployment
  table on [Moderator Tools/Automoderator](https://www.mediawiki.org/wiki/Moderator_Tools/Automoderator#Usage).

**Not collected, kept by hand on the page:** the number of Wikipedias with an AI policy (28, from
[Policies by project](https://meta.wikimedia.org/wiki/Artificial_intelligence/Policies_by_project)) and
the Toolhub Evolved tool count (~80; its search matches substrings, so the count needs judgment).

### The data pages

`Latest.tab` and `History.tab` have four columns:

| date | metric | wiki | value |
|---|---|---|---|
| 2026-11-09 | content_translation | en | 67929 |
| 2026-11-09 | content_translation | total | 1213120 |
| 2026-11-09 | content_translation | wikis_nonzero | 15 |
| 2026-11-09 | content_translation | wikis_checked | 15 |

`date` is the day of the run (UTC). `value` is empty when that wiki couldn't be read.

`Totals.tab` has a `date` column and then one column per metric. Each holds that week's `total`.

All three pages are CC0 and are filed in
[Category:Wikimedia-related tabular data](https://commons.wikimedia.org/wiki/Category:Wikimedia-related_tabular_data).

## Setup

### 1. A bot account, approved on Commons

Commons requires advance permission for **every** bot, whatever its edit rate
([Commons:Bots](https://commons.wikimedia.org/wiki/Commons:Bots)). This step gates everything else,
so start it first.

1. Create a separate account for the bot (for example *AlaexisBot*; check the name is free), and put
   `{{Bot|Alaexis}}` on its user page with a line on what it does and a link to this repo.
2. Make a few test edits by hand from it, e.g. create the three `Data:Wikimedia AI/*.tab` pages from
   a `--dry-run` output (step 4).
3. File a request at [Commons:Bots/Requests](https://commons.wikimedia.org/wiki/Commons:Bots/Requests).
   Suggested text:

   > **Operator:** Alaexis · **Task:** once a week, update three tabular data pages,
   > `Data:Wikimedia AI/Latest.tab`, `History.tab` and `Totals.tab`, with public counts of AI-related
   > change tags, search results and bot edit counts on Wikipedias. **Edit rate:** 3 edits a week.
   > **Source:** https://github.com/alex-o-748/wiki-ai-dashboard · Runs on Toolforge.

4. Once approved, log in as the bot and create a bot password at
   [Special:BotPasswords](https://commons.wikimedia.org/wiki/Special:BotPasswords), name `ai-metrics`,
   grants: **Edit existing pages** and **Create, edit, and move pages**. Bot passwords are global, so
   the same one works wherever the account exists. Note the `AlaexisBot@ai-metrics` username and the
   password it shows once.

Until approval comes through, run `--dry-run` and paste the three files by hand.

### 2. A Toolforge tool

You need a Wikimedia developer account and Toolforge membership; then create a tool called
`ai-metrics` at [toolsadmin](https://toolsadmin.wikimedia.org/). See the
[Toolforge quickstart](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Quickstart).

Server-side requests from Wikimedia Cloud Services with a proper User-Agent are exempt from the API
rate limits ([Rate limits](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits)). The job is
polite anyway: one request at a time, a pause between requests, `maxlag=5`, and it honours
`Retry-After`. Run from anywhere else, it does get rate-limited (HTTP 429). It waits and carries on,
but a full run then takes much longer.

### 3. Deploy

```bash
ssh login.toolforge.org
```

```bash
become ai-metrics
```

```bash
git clone https://github.com/alex-o-748/wiki-ai-dashboard.git
```

```bash
toolforge envvars create AI_METRICS_BOT_USER 'AlaexisBot@ai-metrics'
```

```bash
toolforge envvars create AI_METRICS_BOT_PASSWORD 'the-bot-password'
```

Build the virtualenv inside the same image the job will use (list images with `toolforge jobs images`
and swap `python3.11` for the newest Python there):

```bash
toolforge jobs run bootstrap --image python3.11 --wait --command "cd ~/wiki-ai-dashboard && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
```

### 4. Test

```bash
toolforge jobs run dryrun --image python3.11 --wait --command "cd ~/wiki-ai-dashboard && venv/bin/python collect.py --dry-run"
```

```bash
toolforge jobs logs dryrun
```

The log ends with one line per metric (`total=… nonzero=…/…`, where the second number is
`wikis_checked`, plus `failed=…` if any wiki couldn't be read). The three pages are in
`~/wiki-ai-dashboard/out/`. Check a few numbers against Special:Tags before going further.

For a quick test on two wikis:

```bash
toolforge jobs run quicktest --image python3.11 --wait --command "cd ~/wiki-ai-dashboard && venv/bin/python collect.py --dry-run --only en,pl --skip-search"
```

### 5. Schedule

Weekly, Monday 05:23 UTC:

```bash
toolforge jobs run ai-metrics --image python3.11 --schedule "23 5 * * 1" --command "cd ~/wiki-ai-dashboard && venv/bin/python collect.py"
```

A full run makes about 120 requests and takes a few minutes. `--only` and `--skip-search` refuse to
run without `--dry-run`, so a partial run can never overwrite the real totals.

### 6. Wire up the Meta page

1. Create [Module:AI dashboard](https://meta.wikimedia.org/wiki/Module:AI_dashboard) on Meta with the
   contents of [`meta/Module_AI_dashboard.lua`](meta/Module_AI_dashboard.lua).
2. In the draft, replace each hand-typed `{{formatnum:NNN}}` with an `#invoke`, and each date with
   `{{#invoke:AI dashboard|date}}`. [`meta/usage.wikitext`](meta/usage.wikitext) has every tile and
   table from the draft with the numbers already swapped.
3. Link the data from each tile's source line: `[[:c:Data:Wikimedia AI/Latest.tab|data]]`.

The module in short:

| Call | Gives |
|---|---|
| `{{#invoke:AI dashboard\|value\|content_translation}}` | the total, formatted: 1,213,120 |
| `{{#invoke:AI dashboard\|value\|content_translation\|en}}` | one wiki |
| `{{#invoke:AI dashboard\|value\|community_ai_filter\|wikis_nonzero}}` | wikis above 0 (also `wikis_checked`) |
| `…\|zero=–` | a dash instead of 0 |
| `…\|link=URL` | the number linked to URL, except when it is 0 or missing |
| `…\|raw=1` | digits only, for `#expr` |
| `{{#invoke:AI dashboard\|date}}` | the date of the data: 9 November 2026 |

A missing value shows as `–`. A metric name that doesn't exist shows an error.

The page updates when its cache refreshes. A null edit or `?action=purge` shows the new numbers at once.

## Maintenance

- **Add a wiki, tag or bot:** edit `config.json`, push, then `git pull` in `~/wiki-ai-dashboard`
  on Toolforge. Totals.tab keeps old columns and adds new ones, so history isn't lost.
- **A wiki renames its filter tag:** that wiki's count drops to 0 (it shows as `wikis_nonzero`
  falling), and the log has a `WARNING` that the tag isn't defined there. Update the tag name in
  `config.json`.
- **A bot account is renamed:** its value is left empty, with a `WARNING`.
- **Failures:** if a wiki fails after retries, its value is left empty, the total excludes it, and the
  log says so in a `WARNING` line. Search sometimes answers "too busy"; the job waits and retries.
  If more than a quarter of all values fail (an outage, say), the job saves nothing and exits with
  an error, so the pages keep last week's numbers.
- **Someone edits a data page by hand:** the job reads each page right before saving it and refuses to
  save over an edit made in between. A hand edit made before the run is kept in History.tab and
  Totals.tab. Latest.tab is rewritten every week.
- **Re-running on the same day** replaces that day's rows rather than adding a second set.
- **History.tab size:** each run adds about 10 KB, and a data page holds 2 MB, so it fills in about
  3½ years at one run a week. The job warns at 85% full. Before then, archive it: copy the page to
  `Data:Wikimedia AI/History 2026–2029.tab`, then cut History.tab's `data` down to the last few
  weeks.

## Development

```bash
python3 -m unittest discover tests
```

The tests need no network. The Lua tests run the module in Lua 5.1 (the version Scribunto uses)
through [lupa](https://pypi.org/project/lupa/). They are skipped if lupa isn't installed
(`pip install lupa`).

## Candidates not yet collected

- English Wikipedia's `AI-generated source` tag, and `app-ai-assist`, which exists on every wiki.
- Edit counts of anti-vandalism tools that use ORES scores (Huggle, SWViewer). Their tags count
  tool use, not ORES use, so they need careful labelling.
