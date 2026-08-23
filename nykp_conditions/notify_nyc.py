"""Notify NYC waterbody advisories (e.g. sewage discharge) from the city RSS feed."""

from argparse import ArgumentParser

from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import pendulum

from .utils.scripts import try_main
from .utils.slack import NykpSlackChannels, post_text_attachment


NOTIFY_NYC_URL = 'https://feeds.everbridge.net/feeds/453003085617722/rss/rss.xml'
WATERBODY_ADVISORY = 'Waterbody Advisory'
ALERTS_FIELD = 'entries'
NOTIFY_DATE_FMT = '%m/%d/%Y %H:%M:%S'
NOTIFY_NYC_TZ = ZoneInfo('America/New_York')


def _parse_local(s: str) -> datetime:
    """Parse a timestamp, reading it as NYC-local unless it carries an offset."""
    return pendulum.parse(s, strict=False, tz=NOTIFY_NYC_TZ)


def find_links(s: str) -> list[str]:
    """Pull bare http(s) URLs out of an advisory summary."""
    return [i for i in s.split() if i.startswith('http://') or i.startswith('https://')]


@dataclass
class NotifyAlert:
    title: str
    published: datetime
    summary: str

    def __post_init__(self):
        if isinstance(self.published, str):
            try:
                self.published = datetime.strptime(self.published, NOTIFY_DATE_FMT)
            except ValueError:
                # pendulum assumes UTC for a naive string unless given a tz,
                # which would read these NYC-local stamps four hours early.
                self.published = _parse_local(self.published)
        # Feed timestamps are local to NYC and carry no offset.
        if self.published is not None and self.published.tzinfo is None:
            self.published = self.published.replace(tzinfo=NOTIFY_NYC_TZ)

    @classmethod
    def parse(cls, entry: dict) -> 'NotifyAlert':
        """Build an alert from a feedparser entry, ignoring extra fields.

        Missing text comes back as '' rather than None. The feed carries every
        NYC alert, so one malformed entry of any kind -- a traffic notice, say --
        used to take down the whole waterbody section.
        """
        kws = {f.name: entry.get(f.name) for f in fields(cls)}
        for field in ('title', 'summary'):
            kws[field] = kws[field] or ''
        return cls(**kws)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Attach the Notify NYC timezone to a naive datetime so comparisons work."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=NOTIFY_NYC_TZ)
    return dt


def get_waterbody_advisories(
        start_dt: datetime | None = None,
        end_dt: datetime | None = None,
        url: str = NOTIFY_NYC_URL,
) -> list[NotifyAlert]:
    """Fetch the Notify NYC feed and return waterbody advisories in the window."""
    start_dt = _as_aware(start_dt)
    end_dt = _as_aware(end_dt)

    def f(alert: NotifyAlert) -> bool:
        if WATERBODY_ADVISORY not in alert.title:
            return False
        if alert.published is None:
            # Undateable, so it can't be placed in a requested window.
            return not (start_dt or end_dt)
        if start_dt and alert.published < start_dt:
            return False
        if end_dt and alert.published > end_dt:
            return False
        return True

    feed = feedparser.parse(url)
    alerts = map(NotifyAlert.parse, feed[ALERTS_FIELD])
    return list(filter(f, alerts))


def post_waterbody_advisories(
        channel: str,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        days=1
):
    """Post each waterbody advisory from the last `days` days to Slack."""
    if isinstance(end_time, str):
        end_time = _parse_local(end_time)
    elif end_time is None:
        end_time = datetime.now(tz=NOTIFY_NYC_TZ)

    if isinstance(start_time, str):
        start_time = _parse_local(start_time)
    elif start_time is None:
        start_time = end_time - timedelta(days=days)

    advisories = get_waterbody_advisories(start_dt=start_time, end_dt=end_time)
    for advisory in advisories:
        pretext = f"{advisory.title} ({advisory.published})"
        links = find_links(advisory.summary)
        if links:
            pretext += '\nLinks:\n' + '\n'.join(links)
        post_text_attachment(pretext=pretext, text=advisory.summary, channel=channel)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--channel', type=str, default=None)
    parser.add_argument('--days', type=int, default=1)
    return parser


def main(args):
    if args.channel is not None:
        channel = args.channel
    else:
        channel = NykpSlackChannels.test_python_api
    post_waterbody_advisories(channel, days=args.days)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)
