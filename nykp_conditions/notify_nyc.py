"""Notify NYC waterbody advisories (e.g. sewage discharge) from the city RSS feed."""

from argparse import ArgumentParser

from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import List, Union

import feedparser
import pendulum
import pytz

from utils.scripts import try_main
from utils.slack import NykpSlackChannels, post_text_attachment


NOTIFY_NYC_URL = 'https://a858-nycnotify.nyc.gov/RSS/NotifyNYC?lang=en'
WATERBODY_ADVISORY = 'Waterbody Advisory'
ALERTS_FIELD = 'entries'
NOTIFY_DATE_FMT = '%m/%d/%Y %H:%M:%S'
NOTIFY_NYC_TZ = pytz.timezone('America/New_York')


def find_links(s: str) -> List[str]:
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
                naive = datetime.strptime(self.published, NOTIFY_DATE_FMT)
            except ValueError:
                self.published = pendulum.parse(self.published)
            else:
                # localize(), not replace(tzinfo=...): pytz zones default to
                # their LMT offset (-4:56 for New York) until localized.
                self.published = NOTIFY_NYC_TZ.localize(naive)
        if self.published is not None and self.published.tzinfo is None:
            self.published = NOTIFY_NYC_TZ.localize(self.published)

    @classmethod
    def parse(cls, entry: dict) -> 'NotifyAlert':
        """Build an alert from a feedparser entry, ignoring extra fields."""
        field_names = [f.name for f in fields(cls)]
        kws = {field: entry.get(field) for field in field_names}
        return cls(**kws)


def _as_aware(dt: Union[datetime, None]) -> Union[datetime, None]:
    """Attach the Notify NYC timezone to a naive datetime so comparisons work."""
    if dt is not None and dt.tzinfo is None:
        return NOTIFY_NYC_TZ.localize(dt)
    return dt


def get_waterbody_advisories(
        start_dt: Union[datetime, None] = None,
        end_dt: Union[datetime, None] = None,
        url: str = NOTIFY_NYC_URL,
) -> List[NotifyAlert]:
    """Fetch the Notify NYC feed and return waterbody advisories in the window."""
    start_dt = _as_aware(start_dt)
    end_dt = _as_aware(end_dt)

    def f(alert: NotifyAlert) -> bool:
        if WATERBODY_ADVISORY not in alert.title:
            return False
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
        start_time: Union[datetime, str, None] = None,
        end_time: Union[datetime, str, None] = None,
        days=1
):
    """Post each waterbody advisory from the last `days` days to Slack."""
    if isinstance(end_time, str):
        end_time = pendulum.parse(end_time)
    elif end_time is None:
        end_time = datetime.now(tz=pytz.timezone('America/New_York'))

    if isinstance(start_time, str):
        start_time = pendulum.parse(start_time)
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
