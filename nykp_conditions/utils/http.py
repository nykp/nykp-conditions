"""HTTP fetches that retry on transient upstream failures.

Drop-in stand-ins for `urllib.request.urlopen`/`urlretrieve`. Every source we
scrape (NWS, NOAA, DEP) occasionally answers a 500 or drops the connection, and
a single bad response shouldn't cost the morning's post a whole section.

The response body is read inside the retry, so a connection dropped mid-body
is retried too; `urlopen` hands back the buffered body as a file-like object.
Each attempt also gets a timeout, so a hung server fails over to the next
attempt instead of eating the workflow's time budget.
"""

import http.client
import io
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.request import urlopen as _urlopen

# Seconds to wait before each retry; the request is tried len + 1 times.
RETRY_DELAYS = (5, 15, 30)
TIMEOUT = 30

# Status codes that mean "try again later" rather than "this request is wrong".
RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _is_retriable(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):  # Subclass of URLError, so check it first
        return exc.code in RETRIABLE_STATUS_CODES
    return isinstance(exc, (URLError, TimeoutError, ConnectionError, http.client.HTTPException))


def _fetch(url: str | Request) -> tuple[bytes, http.client.HTTPMessage]:
    """Body and headers of a response, retrying transient failures."""
    attempts = len(RETRY_DELAYS) + 1
    for attempt, delay in enumerate((0,) + RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            with _urlopen(url, timeout=TIMEOUT) as resp:
                return resp.read(), resp.headers
        except Exception as exc:
            if not _is_retriable(exc) or attempt == attempts:
                raise
            full_url = url.full_url if isinstance(url, Request) else url
            print(f"Fetch failed ({exc}) for {full_url} "
                  f"(attempt {attempt}/{attempts}); retrying in {RETRY_DELAYS[attempt - 1]}s")


def urlopen(url: str | Request) -> io.BytesIO:
    """Fetch a URL, returning its body as a file-like object."""
    body, _ = _fetch(url)
    return io.BytesIO(body)


def urlretrieve(url: str | Request, path: str) -> tuple[str, http.client.HTTPMessage]:
    """Download a URL to `path`, returning (path, headers) like the stdlib's."""
    body, headers = _fetch(url)
    with open(path, 'wb') as f:
        f.write(body)
    return path, headers
