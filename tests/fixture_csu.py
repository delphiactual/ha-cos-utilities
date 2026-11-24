"""A stand-in for CSU that serves recorded responses by request window.

FixtureSession takes the place of the aiohttp session the client uses, so the real
client code (windows, retries, parsing, de-duplication) runs unchanged on real
responses. The fixtures come from scripts/record_fixtures.py and
scripts/scrub_fixtures.py; nothing here ever touches the network.

A request for a window that was not recorded raises UnrecordedWindow rather than
guessing. A test that walks past the recorded weeks opts in to empty_outside=True,
which answers those with the recorded empty reply, like the end of the history.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from conftest import FakeResponse
from conftest import _Call

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_PATHS = {
    "/rest/oauth/token": "token",
    "/rest/account/list/": "accounts",
    "/rest/account/services/": "services",
}
SERVICE_ALIASES = {"E-": "elec", "G-": "gas", "W-": "water"}


class UnrecordedWindow(AssertionError):
    """The code asked for a window that was never recorded."""


class FixtureSession:
    def __init__(self, fixtures: Path = FIXTURES, *, empty_outside: bool = False):
        self.dir = Path(fixtures)
        self.index = json.loads((self.dir / "index.json").read_text(encoding="utf-8"))
        self.windows = {
            (w["url_path"], w["meter"], w["fromDate"], w["toDate"]): w
            for w in self.index["windows"]
        }
        self.empty_outside = empty_outside
        self.requests: list[tuple[str, str, str, str]] = []
        self.calls: dict[str, int] = defaultdict(int)

    @property
    def captured_at(self) -> datetime:
        """When the capture was made (America/Denver); freeze the clock here."""
        return datetime.fromisoformat(self.index["captured_at_denver"])

    def _read(self, name: str) -> str:
        data = (self.dir / name).read_bytes()
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        return data.decode("utf-8")

    def _empty_reply(self) -> str:
        for window in self.index["windows"]:
            if window["rows"] == 0 and window["status"] == 200:
                return self._read(window["file"])
        return json.dumps({"history": []})

    def post(self, url: str, **kwargs):
        path = urlparse(url).path
        self.calls[path] += 1
        if path in LOGIN_PATHS:
            name = self.index["login"][LOGIN_PATHS[path]]
            return _Call(FakeResponse(200, body=self._read(name)))
        body = json.loads(kwargs["data"])
        meter = SERVICE_ALIASES.get(str(body.get("serviceNumber"))[:2], "-")
        key = (path, meter, body.get("fromDate", ""), body.get("toDate", ""))
        self.requests.append(key)
        window = self.windows.get(key)
        if window:
            return _Call(
                FakeResponse(window["status"], body=self._read(window["file"]))
            )
        if self.empty_outside:
            return _Call(FakeResponse(200, body=self._empty_reply()))
        raise UnrecordedWindow(
            f"no recording for {path} {meter} {key[2]} to {key[3]}; "
            "record it, or pass empty_outside=True to treat it as past the history"
        )
