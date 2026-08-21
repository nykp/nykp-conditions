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

> **Note:** the modules import each other flatly (`from utils.slack import …`),
> so scripts must be run **from inside `nykp_conditions/`**. Running
> `python nykp_conditions/post_conditions.py` from the repo root will fail on
> the imports.

```bash
pip install -e '.[dev]'
cd nykp_conditions

# Everything, to the test channel (the default)
python post_conditions.py

# A specific channel, two days of currents
python post_conditions.py --channel hudson-conditions --days 2

# Skip sections you don't want while iterating
python post_conditions.py --currents false --forecast false

# Attach the water temperature timeseries plot (off by default)
python post_conditions.py --water-temp-plot true
```

Each source module is also runnable on its own:

```bash
cd nykp_conditions
python noaa_currents.py --station NYH1928_12 --range 1w
python water_temps.py --channel test-python-api --plot true
python nws_forecast.py --keep          # Leaves the downloaded plot in cache/
python nws_precip.py --station KNYC --hours 24
python notify_nyc.py --days 7
python dep_advisory.py --post-when-clear   # Posts even with no advisory
```

Every script accepts `--pdb` (break on entry) and `--postmortem` (drop into
pdb on an exception).

## Tests

```bash
pytest
```

Tests live in `tests/` and cover parsing, formatting, and error handling with
no network access — HTTP calls, the Slack client, and the RSS feed are all
stubbed. `test_post_conditions.py` stubs every section poster through an
autouse fixture: a section left unstubbed there would reach the real upstream
sources and post to a real Slack channel.

`tests/conftest.py` puts `nykp_conditions/` on `sys.path` so the tests import
the modules the same flat way the scripts do.

CI runs them on every push and pull request against Python 3.11 and 3.13
(`.github/workflows/tests.yml`) — the floor declared in `pyproject.toml` and
the version the daily job actually uses.

## Layout

```
nykp_conditions/
  post_conditions.py   Entry point for the daily post
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
