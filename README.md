# NYKP conditions

Posts a daily Hudson River conditions summary to the New York Kayak Polo Slack.

Every morning a GitHub Actions run collects current predictions, water
temperature, the NWS forecast, and any Notify NYC waterbody advisories, and
posts them to `#hudson-conditions`.

## What gets posted

| Section | Source | Module |
| --- | --- | --- |
| Tidal current predictions (slack / max flood / max ebb) | NOAA Tides & Currents | `noaa_currents.py` |
| Waterbody advisories (e.g. sewage discharge) | Notify NYC RSS feed | `notify_nyc.py` |
| Water temperature, last 36 hours, with a plot | NOAA Tides & Currents | `water_temps.py` |
| Forecast meteogram and text summary | forecast.weather.gov (scraped) | `nws_forecast.py` |
| Observed precipitation, last 24 hours, with a plot | api.weather.gov station observations | `nws_precip.py` |

Each section posts independently. If one upstream source is down the others
still go out; the run then fails at the end so the failure is visible in the
Actions log rather than silently swallowed.

## How it runs

`.github/workflows/daily-conditions.yml` runs `post_conditions.py` on a cron
schedule at 12:30 UTC (8:30am EDT / 7:30am EST), and can also be triggered
manually via **workflow_dispatch**, where you can choose the target channel.

GitHub can delay scheduled runs during peak times, so the exact posting time
drifts by a few minutes.

### Secrets

The workflow needs one repository secret:

- `SLACK_BOT_TOKEN` — the bot token for the Slack app (`xoxb-…`).

The bot must be a member of any channel it posts to, and the token needs
`chat:write`, `files:write`, and `channels:read` (used to resolve a channel
name to an ID).

Locally, `utils/secrets.py` reads `$SLACK_BOT_TOKEN` first and otherwise falls
back to `.secrets/slack_app_bot_token` (gitignored). Either works.

## Running locally

Requires **Python 3.11+**; the scheduled job and local development both run on
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
```

Each source module is also runnable on its own:

```bash
cd nykp_conditions
python noaa_currents.py --station NYH1928_12 --range 1w
python water_temps.py --channel test-python-api
python nws_forecast.py --keep          # Leaves the downloaded plot in cache/
python notify_nyc.py --days 7
```

Every script accepts `--pdb` (break on entry) and `--postmortem` (drop into
pdb on an exception).

## Tests

```bash
pytest
```

Tests live in `tests/` and cover parsing, formatting, and error handling with
no network access — HTTP calls, the Slack client, and the RSS feed are all
stubbed. `tests/conftest.py` puts `nykp_conditions/` on `sys.path` so the tests
import the modules the same flat way the scripts do.

CI runs them on every push and pull request against Python 3.11 and 3.13
(`.github/workflows/tests.yml`) — the floor declared in `pyproject.toml` and
the version the daily job actually uses.

## Layout

```
nykp_conditions/
  post_conditions.py   Entry point for the daily post
  noaa_currents.py     Tidal current predictions
  water_temps.py       Water temperature + plot
  nws_forecast.py      NWS meteogram + text forecast
  notify_nyc.py        Notify NYC waterbody advisories
  nws_precip.py        Observed precipitation + plot
  utils/
    slack.py           Posting, retries on transient errors, channel lookup
    secrets.py         Token from env var or .secrets/
    scripts.py         Shared CLI plumbing (str2bool, try_main)
    data.py, geo.py, plot.py, units.py
tests/                 pytest suite
notebooks/             Exploratory notebooks (not used by the job)
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
