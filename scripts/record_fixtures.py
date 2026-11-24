"""Record CSU's raw responses so tests can use real ones (dev only, not shipped).

Reads CSU_USERNAME / CSU_PASSWORD from the environment (and CSU_MFA for a one-time
code; if CSU asks for one and it is not set, this prompts), logs in with the
integration's own client, and calls the same endpoints with the same windows the
integration does: meters, hourly (detail/day) and daily (detail/month) for
electric and gas. Every response is saved byte for byte, one file per request
window, under fixtures_raw/<capture time>/ (gitignored) together with the request
body that produced it (password and codes redacted).

The raw files hold account data. Never commit them: run scripts/scrub_fixtures.py
on the capture directory and commit only what it writes to tests/fixtures/.

    pip install aiohttp arrow python-dateutil
    python scripts/record_fixtures.py

A full pull takes about 25 minutes, because the hourly history is fetched in
7-day windows the way the history rebuild does.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
import types
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = ROOT / "custom_components" / "csu"
RAW_DIR = ROOT / "fixtures_raw"

REDACTED = "<redacted>"
SECRET_KEYS = {"password", "mfa", "mfaToken"}
SERVICE_ALIASES = {"E-": "elec", "G-": "gas", "W-": "water"}
# The rebuild stops after this many empty windows in a row (when the client
# supports it), which is also how far past the first read the capture goes.
EMPTY_WINDOWS = 5


def load_csu():
    """Import csu.py without running the package __init__ (needs Home Assistant)."""
    pkg = types.ModuleType("csu_pkg")
    pkg.__path__ = [str(COMPONENT_DIR)]
    sys.modules["csu_pkg"] = pkg
    return importlib.import_module("csu_pkg.csu"), importlib.import_module(
        "csu_pkg.exceptions"
    )


def alias_of(request: dict) -> str:
    """elec, gas or water for a usage request, from its service number prefix."""
    return SERVICE_ALIASES.get(str(request.get("serviceNumber") or "")[:2], "-")


def redact_request(data) -> dict:
    """The request body for the log, without anything that logs in."""
    if isinstance(data, str):
        try:
            body = json.loads(data)
        except ValueError:
            return {"body": REDACTED}
        if isinstance(body, dict):
            return {k: REDACTED if k in SECRET_KEYS else v for k, v in body.items()}
        return {"body": REDACTED}
    # The token request is a form with the username and password in it.
    return {"form": REDACTED}


class _Replay:
    """What the client sees: the recorded bytes, served like an aiohttp response."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode("utf-8")

    async def json(self, content_type=None, **_) -> object:
        return json.loads(self._body)


class _Call:
    def __init__(self, recorder: Recorder, url: str, kwargs: dict) -> None:
        self._recorder = recorder
        self._url = url
        self._kwargs = dict(kwargs)

    async def __aenter__(self) -> _Replay:
        # Make the real call without raising, so error bodies are recorded too,
        # then raise afterwards the way raise_for_status would have.
        raise_for_status = self._kwargs.pop("raise_for_status", False)
        async with self._recorder.real.post(self._url, **self._kwargs) as resp:
            body = await resp.read()
            status = resp.status
            info = resp.request_info
        self._recorder.save(self._url, self._kwargs.get("data"), status, body)
        if raise_for_status and status >= 400:
            raise aiohttp.ClientResponseError(info, (), status=status)
        return _Replay(status, body)

    async def __aexit__(self, *exc) -> bool:
        return False


class Recorder:
    """A session the client can use; it forwards to the real one and keeps a copy."""

    def __init__(self, real, out_dir: Path) -> None:
        self.real = real
        self.out_dir = out_dir
        self.entries: list[dict] = []
        out_dir.mkdir(parents=True, exist_ok=True)

    def post(self, url: str, **kwargs) -> _Call:
        return _Call(self, url, kwargs)

    def save(self, url: str, data, status: int, body: bytes) -> None:
        number = len(self.entries) + 1
        path = urlparse(url).path
        request = redact_request(data)
        endpoint = path.strip("/").split("/")[-1] or "root"
        alias = alias_of(request)
        from_date = str(request.get("fromDate", ""))
        to_date = str(request.get("toDate", ""))
        stem = f"{number:04d}_{endpoint}_{alias}"
        if from_date:
            stem += f"_{from_date[:10]}_{to_date[:10]}"
        (self.out_dir / f"{stem}.json").write_bytes(body)
        (self.out_dir / f"{stem}.request.json").write_text(
            json.dumps({"url": url, "request": request, "status": status}, indent=2),
            encoding="utf-8",
        )
        rows = None
        try:
            history = json.loads(body).get("history")
            rows = len(history) if isinstance(history, list) else None
        except (ValueError, AttributeError):
            pass
        self.entries.append(
            {
                "n": number,
                "url_path": path,
                "alias": alias,
                "fromDate": from_date,
                "toDate": to_date,
                "status": status,
                "rows": rows,
                "bytes": len(body),
                "file": f"{stem}.json",
                "request_file": f"{stem}.request.json",
            }
        )
        self.write_index()

    def write_index(self, extra: dict | None = None) -> None:
        index = {"entries": self.entries, **(extra or {})}
        (self.out_dir / "capture.json").write_text(
            json.dumps(index, indent=2), encoding="utf-8"
        )


async def log_in(api, errors) -> None:
    """Log in, asking for the emailed one-time code if CSU wants one."""
    try:
        await api.async_login()
    except errors.MfaRequired as err:
        await api.async_send_mfa_code(err.mfa_token)
        code = os.environ.get("CSU_MFA") or input("One-time code CSU emailed: ")
        api.mfa = code.strip()
        await api.async_login()


async def record(api, csu, recorder: Recorder) -> None:
    now = datetime.now(csu.LOCAL_TZ)
    day = timedelta(days=1)
    kinds = csu.AggregateType
    walk = {"window_days": 7, "strict": True}
    if "stop_after_empty" in inspect.signature(api.async_get_usage_reads).parameters:
        walk["stop_after_empty"] = EMPTY_WINDOWS
    await api.async_get_meters()
    meters = [m for m in api.meters if m.meter_type.name in ("ELEC", "GAS")]
    print(f"Meters found: {sorted(m.meter_type.name for m in api.meters)}")
    for meter in meters:
        name = meter.meter_type.name
        # The same walks the history rebuild makes, ending tomorrow.
        print(f"{name}: hourly history in 7-day windows (the long part)...")
        reads = await api.async_get_usage_reads(
            meter, kinds.HOUR, now - timedelta(days=3650), now + day, **walk
        )
        print(f"{name}: {len(reads)} hourly reads")
        print(f"{name}: daily history, 2 years...")
        await api.async_get_usage_reads(
            meter, kinds.DAY, now - timedelta(days=730), now + day, strict=True
        )
        # What the regular update asks for, and the narrow windows around today.
        print(f"{name}: the live update's windows and today's daily row...")
        await api.async_get_usage_reads(meter, kinds.HOUR, now - 3 * day, now)
        await api.async_get_usage_reads(meter, kinds.DAY, now - 30 * day, now)
        await api.async_get_usage_reads(meter, kinds.DAY, now - day, now + day)
        await api.async_get_usage_reads(meter, kinds.DAY, now, now + day)


async def main() -> int:
    username = os.environ.get("CSU_USERNAME")
    password = os.environ.get("CSU_PASSWORD")
    if not username or not password:
        print("Set CSU_USERNAME and CSU_PASSWORD (and CSU_MFA if you have a code).")
        return 1
    csu, errors = load_csu()
    started = datetime.now(timezone.utc)
    out_dir = RAW_DIR / started.strftime("%Y%m%dT%H%M%SZ")
    async with aiohttp.ClientSession() as session:
        recorder = Recorder(session, out_dir)
        api = csu.CSU(recorder, username, password)
        print(f"Recording to {out_dir} (raw account data: never commit it)")
        await log_in(api, errors)
        await record(api, csu, recorder)
    finished = datetime.now(timezone.utc)
    recorder.write_index(
        {
            "captured_at_utc": started.isoformat(),
            "captured_at_denver": started.astimezone(csu.LOCAL_TZ).isoformat(),
            "finished_at_utc": finished.isoformat(),
        }
    )
    print(f"Done: {len(recorder.entries)} responses in {out_dir}")
    print(f"Next: python scripts/scrub_fixtures.py {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
