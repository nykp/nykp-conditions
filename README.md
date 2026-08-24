# NYKP conditions

Posts a daily Hudson River conditions summary to the New York Kayak Polo Slack.

Every morning a GitHub Actions run collects current predictions, water
temperature, the NWS forecast, observed precipitation, and any waterbody
advisories, and posts them to `#hudson-conditions`.

## What gets posted

Listed in the order they're posted:

| Section | Source | Module |
| --- | --- | --- |
| Tidal current predictions (slack / max flood / max ebb) | NOAA Tides & Currents | `noaa_currents.py` |
| Water temperature range (low/high and when), last 36 hours | NOAA Tides & Currents | `water_temps.py` |
| Forecast meteogram and text summary | forecast.weather.gov (scraped) | `nws_forecast.py` |
| Observed precipitation, last 24 hours, with a plot | api.weather.gov station observations | `nws_precip.py` |
| Waterbody advisories (e.g. sewage discharge) | Notify NYC RSS feed | `notify_nyc.py` |
| Advisory status for the Hudson off Pier 96, with a citywide map | NYC DEP Waterbody Advisory Dashboard | `dep_advisory.py` |

Each section posts independently. If one upstream source is down the others
still go out; the run then fails at the end so the failure is visible in the
Actions log rather than silently swallowed.

The DEP section is the one exception to "every section posts every day": it
stays quiet unless the Hudson is actually on advisory, since a standing "no
advisory" line would be noise on most days. Pass `--post-when-clear` when
running it by hand to see the all-clear.

The post always names the 24-hour and 96-hour rainfall totals at the gauge the
advisory is keyed to, which is what explains an advisory that outlasts the
rain. The gauge's hourly *chart* is opt-in (`--dep-rainfall true` on the daily
run, `--rainfall true` on the script), since `nws_precip.py` already posts an
observed-precipitation plot daily. When it's on, both images go out as a single
Slack message.

### The advisory map

The dashboard's map can't be downloaded. It's an ArcGIS JavaScript layer that
paints vector features onto a canvas in the browser, and the FeatureServer
behind it advertises `Query` but not `Map`, so there's no `export` image
operation to hit either. `dep_advisory.py` pulls the waterbody polygons
instead — generalized, which takes them from 10 MB to ~140 KB — joins them to
the live advisory status on `FID`, and draws the map itself.

Two sources, deliberately kept separate: Notify NYC tells us what the city
*announced*, while the DEP dashboard carries the hour-by-hour status of all 45
waterbodies whether or not anything was announced.

## How it runs

`.github/workflows/daily-conditions.yml` runs `post_conditions.py`. Its only
trigger is **workflow_dispatch** — there is deliberately no GitHub `schedule:`
cron in the workflow.

The daily run is started externally by a [cron-job.org](https://cron-job.org)
job at 8:30am `America/New_York`, which POSTs to the workflow dispatch API:

```
POST https://api.github.com/repos/nykp/noaa-currents/actions/workflows/daily-conditions.yml/dispatches
{"ref": "main", "inputs": {"channel": "hudson-conditions"}}
```

It authenticates with a fine-grained personal access token scoped to this
repository with **Actions: read and write** — the token's only capability is
starting workflow runs here. The same endpoint backs the "Run workflow" button
in the Actions tab, so manual runs still work and can target a different
channel.

GitHub's own `schedule:` trigger was dropped because it ran two to three hours
late in practice (10:58am and 11:22am EDT against an 8:30am cron), well past the
~8am window when Notify NYC publishes waterbody advisories after heavy rain.
Scheduled events are the lowest-priority queue on shared runners and carry no
delivery guarantee. Dispatched runs start within seconds, and an external
schedule also follows DST, which a UTC cron cannot.

The tradeoff is that nothing else fires this workflow, and the two ways that
can silently stop are both invisible from the Actions tab: the PAT expiring,
and cron-job.org disabling a job after repeated failures. Keep failure
notifications enabled on the cron job.

### Secrets

The workflow needs one repository secret:

- `SLACK_BOT_TOKEN` — the bot token for the Slack app (`xoxb-…`).

(The GitHub PAT that triggers the run isn't stored here — it lives in the
cron-job.org job's `Authorization` header.)

The bot must be a member of any channel it posts to, and the token needs
`chat:write`, `files:write`, and `channels:read` (used to resolve a channel
name to an ID).

Locally, `utils/secrets.py` reads `$SLACK_BOT_TOKEN` first and otherwise falls
back to `.secrets/slack_app_bot_token` (gitignored). Either works.

## Running locally

Requires **Python 3.11+**; the daily job and local development both run on
3.13. Timezone handling uses the stdlib `zoneinfo`, so there's no `pytz`
dependency.

Installing the package puts a `nykp-conditions` command on the PATH, with one
subcommand per source. Run it from anywhere — no `cd` into the package first.

```bash
pip install -e '.[dev]'

nykp-conditions                 # Lists the commands
nykp-conditions post --help     # A command's own options
```

| Command | What it does |
| --- | --- |
| `post` | Post the full conditions summary to Slack |
| `web-export` | Write the conditions as JSON + plots for the website |
| `currents` | Tidal current predictions |
| `water-temps` | Water temperature range |
| `forecast` | NWS text forecast and meteogram |
| `precip` | Observed precipitation |
| `notify-nyc` | Waterbody advisories announced through Notify NYC |
| `dep-advisory` | DEP waterbody advisory status and citywide map |

```bash
# Everything, to the test channel (the default)
nykp-conditions post

# A specific channel, two days of currents
nykp-conditions post --channel hudson-conditions --days 2

# Skip sections you don't want while iterating
nykp-conditions post --currents false --forecast false

# Attach the water temperature timeseries plot (off by default)
nykp-conditions post --water-temp-plot true

# Individual sources
nykp-conditions currents --station NYH1928_12 --range 1w
nykp-conditions water-temps --channel test-python-api --plot true
nykp-conditions forecast --keep          # Leaves the plot in cache/
nykp-conditions precip --station KNYC --hours 24
nykp-conditions notify-nyc --days 7
nykp-conditions dep-advisory --rainfall true   # Adds the rainfall chart
```

Each subcommand hands off to its module's own `parse_args`/`main`, so there's
one definition of every flag and the dispatcher can't drift from it.
`python -m nykp_conditions.<module>` still works unchanged, and
`python -m nykp_conditions` reaches the dispatcher.

### Using it as a library

The fetch and plot functions are independent of Slack, so another project can
install this package straight from GitHub and reuse them:

```bash
pip install git+https://github.com/nykp/noaa-currents
```

```python
from nykp_conditions.water_temps import get_water_temps, temp_range
from nykp_conditions.dep_advisory import get_advisories, find_advisory
```

Each module layers as **fetch → format → post**: `get_*`/`retrieve_*` return
data, `format_*_post` renders Slack markup, `plot_*_file` writes a PNG, and
`post_*` does the Slack call. A non-Slack consumer uses the first and third and
supplies its own rendering.

Every script accepts `--pdb` (break on entry) and `--postmortem` (drop into
pdb on an exception).

## The website conditions page

`web_export.py` renders the same data as JSON plus plot PNGs, for the conditions
page on [nykayakpolo.org](https://nykayakpolo.org). The website's deploy
workflow installs this package, runs the module, and lets Hugo templates style
the result -- nothing generated is committed to the site repo.

```bash
nykp-conditions web-export \
    --out data/conditions.json \
    --img-dir static/img/conditions \
    --img-url /img/conditions
```

`--img-dir` is where the PNGs are written; `--img-url` is the path they'll be
served at, which is what gets recorded in the JSON. Omit `--img-dir` for a
text-only document.

**Failure handling differs from the Slack post on purpose.** There, a failed
section still exits nonzero so the run goes red and the failure is visible.
Here a nonzero exit would abort the site deploy, so an upstream outage would
block an unrelated content change from publishing. Instead each failed section
is written as `{"status": "unavailable", "error": ...}` for the page to render
honestly, reported as a GitHub Actions `::warning::` annotation, and the run
still exits 0. `unavailable` at the top level lists any sections that failed.

## Tests

```bash
pytest
```

Tests live in `tests/` and cover parsing, formatting, and error handling with
no network access — HTTP calls, the Slack client, and the RSS feed are all
stubbed. `test_post_conditions.py` stubs every section poster through an
autouse fixture: a section left unstubbed there would reach the real upstream
sources and post to a real Slack channel.

The tests import the package by its full path (`import
nykp_conditions.noaa_currents as nc`), the same way any other consumer would.

CI runs them on every push and pull request against Python 3.11 and 3.13
(`.github/workflows/tests.yml`) — the floor declared in `pyproject.toml` and
the version the daily job actually uses.

## Layout

```
nykp_conditions/
  cli.py               The `nykp-conditions` command (subcommand dispatch)
  post_conditions.py   Entry point for the daily post
  web_export.py        JSON + images for the website conditions page
  noaa_currents.py     Tidal current predictions
  water_temps.py       Water temperature range (plot optional)
  nws_forecast.py      NWS meteogram + text forecast
  notify_nyc.py        Notify NYC waterbody advisories
  dep_advisory.py      DEP advisory status + citywide map
  nws_precip.py        Observed precipitation + plot
  utils/
    slack.py           Posting, retries on transient errors, channel lookup
    secrets.py         Token from env var or .secrets/
    scripts.py         Shared CLI plumbing (str2bool, try_main)
    data.py, geo.py, plot.py, units.py
  notebooks/           Exploratory notebooks (untracked, not used by the job)
tests/                 pytest suite
```

## Notes

- Downloaded files (plots, CSVs) go to temp directories that are removed only
  after the upload returns, so a file can't vanish mid-upload. `--keep` on
  `nws_forecast.py` writes to `cache/` instead for debugging.
- Slack uploads retry with backoff on transient errors (`file_update_failed`
  and friends), which show up intermittently on `files_upload_v2`.
- The NWS forecast is scraped from HTML, so it's the most likely thing to
  break if the page layout changes. It raises rather than posting something
  wrong.
- The DEP dashboard's `/api/advisory` intermittently answers **200 with a
  partial or empty list** while the app warms up, then fills in over a minute
  or two. Polled every 10s on 2026-08-23: seven straight empty responses, then
  12, 36, and 45 records. The empty case fails loudly, but a *partial* one is
  worse -- it looks fine and yields a citywide count off a short list ("3 of 12
  waterbodies on advisory" instead of "of 45"), and may not even contain the
  waterbody being asked about.

  `get_complete_advisories` handles this: it reads the expected waterbody count
  from the ArcGIS map layer (`returnCountOnly`, a different host, so a cold
  dashboard can't skew both the list and the check on it) and refetches until
  the list is whole, backing off 5s/15s/30s/60s/60s. If that count can't be
  read it falls back to accepting the first non-empty response that repeats.
  Failing to complete raises, so the section is marked unavailable rather than
  published with a wrong number. Both the Slack post and the web export go
  through it; a healthy dashboard costs about a second.
