"""The recorder, the scrubber and the fixture session, on a made-up capture.

Every identifier below is invented. The real capture never goes through the tests.
"""

import gzip
import importlib.util
import json
import sys
from datetime import date
from datetime import datetime
from pathlib import Path

import aiohttp
import pytest

from fixture_csu import FixtureSession
from fixture_csu import UnrecordedWindow

ROOT = Path(__file__).resolve().parent.parent
HOURLY = "/rest/usage/detail/day"
DAILY = "/rest/usage/detail/month"

ORIGINALS = [
    "REAL-ACCESS-TOKEN-abc123",
    "CUST-778899",
    "Quimby",
    "pat.quimby@example.org",
    "ACCT-4455667",
    "CTX-SVC-9911",
    "12 Imaginary Way",
    "MTR-ELEC-3141",
    "SVC-ELEC-2718",
    "CON-ELEC-1618",
    "MTR-GAS-1414",
    "SVC-GAS-1732",
    "CON-GAS-2236",
]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scrub_mod():
    return load_script("scrub_fixtures")


@pytest.fixture(scope="module")
def record_mod():
    return load_script("record_fixtures")


def _row(label, imp, rec, meter="MTR-ELEC-3141"):
    return {
        "usageDate": label,
        "usageConsumptionValue": imp,
        "netReceivedValue": rec,
        "intervalType": 60,
        "meterNumber": meter,
    }


ELEC_RECENT = json.dumps(
    {
        "history": [
            _row("2026-11-23 09:00", "1.25", "-0.5"),
            _row("2026-11-23 10:00", "0.75", None),
        ]
    }
)
ELEC_MID = json.dumps({"history": [_row("2026-06-02 09:00", "2.0", "-1.0")]})
ELEC_START = json.dumps(
    {
        "history": [
            _row("2023-04-04 15:00", "0.5", None),
            _row("2023-04-04 16:00", "0.4", None),
        ]
    }
)
ELEC_BEFORE = json.dumps(
    {
        "history": [
            _row("2023-03-30 00:00", None, None),
            _row("2023-03-30 01:00", None, None),
        ]
    }
)
EMPTY = json.dumps({"history": []})
GAS_RECENT = json.dumps(
    {"history": [_row("2026-11-23 07:00", "0.03", None, "MTR-GAS-1414")]}
)
DAILY_ELEC = json.dumps(
    {
        "history": [
            {
                "usageDate": "2026-11-22 00:00",
                "usageConsumptionValue": "40.5",
                "netReceivedValue": "-3.0",
                "intervalType": 1440,
            }
        ]
    }
)


def build_capture(root: Path, extra_secret_row: str | None = None) -> Path:
    capture = root / "capture"
    capture.mkdir()
    login = {
        "token": {
            "access_token": "REAL-ACCESS-TOKEN-abc123",
            "expires_in": 3600,
            "token_type": "bearer",
            "user": {
                "customerId": "CUST-778899",
                "firstName": "Pat",
                "lastName": "Quimby",
                "email": "pat.quimby@example.org",
            },
        },
        "accounts": {
            "account": [
                {
                    "accountNumber": "ACCT-4455667",
                    "serviceId": "CTX-SVC-9911",
                    "premiseAddress": "12 Imaginary Way",
                }
            ]
        },
        "services": {
            "accountSummaryType": {
                "servicesForGraph": [
                    {
                        "servDescr": "Net meter MTR-ELEC-3141 (export)",
                        "meterNumber": "MTR-ELEC-3141",
                        "serviceNumber": "E-NET",
                        "serviceId": "SVC-ELEC-2718",
                        "serviceContract": "CON-ELEC-1618",
                        "contractAccountNumber": "ACCT-4455667",
                        "serviceAddress": "12 Imaginary Way",
                        "status": "Active",
                    },
                    {
                        "meterNumber": "MTR-GAS-1414",
                        "serviceNumber": "G-TYPICAL",
                        "serviceId": "SVC-GAS-1732",
                        "serviceContract": "CON-GAS-2236",
                        "contractAccountNumber": "ACCT-4455667",
                        "serviceAddress": "12 Imaginary Way",
                        "status": "Active",
                    },
                ]
            }
        },
    }
    paths = {
        "token": "/rest/oauth/token",
        "accounts": "/rest/account/list/",
        "services": "/rest/account/services/",
    }
    entries = []

    def add(path, alias, from_date, to_date, text, rows=None, status=200):
        number = len(entries) + 1
        name = f"{number:04d}.json"
        (capture / name).write_bytes(text.encode("utf-8"))
        entries.append(
            {
                "n": number,
                "url_path": path,
                "alias": alias,
                "fromDate": from_date,
                "toDate": to_date,
                "status": status,
                "rows": rows,
                "bytes": len(text),
                "file": name,
            }
        )

    for name, body in login.items():
        add(paths[name], "-", "", "", json.dumps(body))
    recent = ELEC_RECENT
    if extra_secret_row:
        recent = json.dumps(
            {
                "history": [
                    *json.loads(ELEC_RECENT)["history"],
                    _row(extra_secret_row, "1", None),
                ]
            }
        )
    add(HOURLY, "elec", "2026-11-18 00:00", "2026-11-25 00:00", recent, 2)
    add(HOURLY, "elec", "2026-06-01 00:00", "2026-06-08 00:00", ELEC_MID, 1)
    add(HOURLY, "elec", "2023-04-01 00:00", "2023-04-08 00:00", ELEC_START, 2)
    add(HOURLY, "elec", "2023-03-25 00:00", "2023-04-01 00:00", ELEC_BEFORE, 2)
    add(HOURLY, "elec", "2023-03-18 00:00", "2023-03-25 00:00", EMPTY, 0)
    add(HOURLY, "gas", "2026-11-18 00:00", "2026-11-25 00:00", GAS_RECENT, 1)
    add(DAILY, "elec", "2026-09-25 00:00", "2026-11-25 00:00", DAILY_ELEC, 1)
    (capture / "capture.json").write_text(
        json.dumps(
            {
                "entries": entries,
                "captured_at_utc": "2026-11-24T12:45:00+00:00",
                "captured_at_denver": "2026-11-24T05:45:00-07:00",
            }
        ),
        encoding="utf-8",
    )
    return capture


@pytest.fixture
def fixtures(tmp_path, scrub_mod):
    out = tmp_path / "fixtures"
    scrub_mod.scrub(build_capture(tmp_path), out)
    return out


def test_scrubbed_output_has_none_of_the_original_values(fixtures):
    text = "".join(
        (
            gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
        ).decode("utf-8")
        for p in fixtures.iterdir()
    )
    for original in ORIGINALS:
        assert original not in text
    assert "Pat" not in text
    assert "fake-customer-1" in text


def test_only_the_wanted_windows_are_kept(fixtures):
    index = json.loads((fixtures / "index.json").read_text(encoding="utf-8"))
    kept = {
        (w["meter"], w["fromDate"][:10], w["toDate"][:10]): w for w in index["windows"]
    }
    assert ("elec", "2026-11-18", "2026-11-25") in kept  # last 7 days
    assert ("elec", "2023-04-01", "2023-04-08") in kept  # service start
    assert ("elec", "2023-03-25", "2023-04-01") in kept  # the null-only week before
    assert ("elec", "2023-03-18", "2023-03-25") in kept  # an empty week
    assert ("elec", "2026-09-25", "2026-11-25") in kept  # daily, with today
    assert ("elec", "2026-06-01", "2026-06-08") not in kept  # nothing special
    assert "service-start" in kept[("elec", "2023-04-01", "2023-04-08")]["tags"]
    assert "empty" in kept[("elec", "2023-03-18", "2023-03-25")]["tags"]
    assert index["captured_at_denver"] == "2026-11-24T05:45:00-07:00"


def test_usage_windows_differ_from_the_capture_only_in_the_swapped_values(fixtures):
    kept = gzip.decompress(
        (fixtures / "elec_hourly_2026-11-18_2026-11-25.json.gz").read_bytes()
    ).decode("utf-8")
    assert kept == ELEC_RECENT.replace("MTR-ELEC-3141", "fake-meter-elec")


def test_a_value_that_survives_the_swap_fails_loudly_and_writes_nothing(
    tmp_path, scrub_mod
):
    capture = build_capture(tmp_path, extra_secret_row="zzz-never-seen-before")
    out = tmp_path / "out"

    with pytest.raises(scrub_mod.LeakError) as err:
        scrub_mod.scrub(capture, out, forbidden=("zzz-never-seen-before",))

    assert not out.exists()
    assert "zzz-never-seen-before" not in str(
        err.value
    )  # names the file, not the value


def test_dst_dates_are_the_most_recent_spring_and_fall_changes(scrub_mod):
    assert scrub_mod.dst_dates(date(2026, 11, 24)) == [
        date(2026, 3, 8),
        date(2026, 11, 1),
    ]


async def test_the_real_client_runs_on_the_fixtures(mod, fixtures):
    session = FixtureSession(fixtures, empty_outside=True)
    api = mod.CSU(session, "user", "pass")

    await api.async_login()
    await api.async_get_meters()
    elec = next(m for m in api.meters if m.meter_type.name == "ELEC")
    reads = await api.async_get_usage_reads(
        elec,
        mod.csu.AggregateType.HOUR,
        datetime(2026, 11, 1),
        datetime(2026, 11, 25),
        window_days=7,
        strict=True,
    )

    assert sorted(m.meter_type.name for m in api.meters) == ["ELEC", "GAS"]
    assert [(r.start_time.hour, r.consumption, r.received) for r in reads] == [
        (8, 1.25, 0.5),
        (9, 0.75, 0.0),
    ]
    assert session.requests[0][1:] == (
        "elec",
        "2026-11-18 00:00",
        "2026-11-25 00:00",
    )


async def test_a_window_nobody_recorded_is_an_error_not_a_guess(mod, fixtures):
    api = mod.CSU(FixtureSession(fixtures), "user", "pass")
    await api.async_login()
    await api.async_get_meters()
    elec = next(m for m in api.meters if m.meter_type.name == "ELEC")

    with pytest.raises(UnrecordedWindow):
        await api.async_get_usage_reads(
            elec,
            mod.csu.AggregateType.HOUR,
            datetime(2026, 11, 1),
            datetime(2026, 11, 25),
            window_days=7,
            strict=True,
        )


class _StubResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.request_info = None

    async def read(self):
        return self._body


class _StubCall:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _StubSession:
    def __init__(self, *responses):
        self._responses = list(responses)

    def post(self, url, **kwargs):
        return _StubCall(self._responses.pop(0))


async def test_the_recorder_keeps_the_bytes_and_hides_the_password(
    tmp_path, record_mod
):
    body = b'{"history": [{"usageDate": "2026-11-23 09:00"}]}'
    recorder = record_mod.Recorder(
        _StubSession(_StubResponse(200, body), _StubResponse(500, b"oops")),
        tmp_path / "raw",
    )
    request = json.dumps(
        {
            "serviceNumber": "E-NET",
            "fromDate": "2026-11-18 00:00",
            "toDate": "2026-11-25 00:00",
            "password": "hunter2",
        }
    )

    async with recorder.post(
        "https://example.test/rest/usage/detail/day",
        data=request,
        raise_for_status=True,
    ) as resp:
        assert await resp.json() == json.loads(body)
    with pytest.raises(aiohttp.ClientResponseError):
        async with recorder.post(
            "https://example.test/rest/usage/detail/day",
            data=request,
            raise_for_status=True,
        ):
            pass

    saved = sorted(p.name for p in (tmp_path / "raw").iterdir())
    assert saved[0].startswith("0001_day_elec_2026-11-18_2026-11-25")
    assert (
        tmp_path / "raw" / "0001_day_elec_2026-11-18_2026-11-25.json"
    ).read_bytes() == body
    logged = (
        tmp_path / "raw" / "0001_day_elec_2026-11-18_2026-11-25.request.json"
    ).read_text(encoding="utf-8")
    assert "hunter2" not in logged
    entries = json.loads(
        (tmp_path / "raw" / "capture.json").read_text(encoding="utf-8")
    )["entries"]
    assert [(e["status"], e["rows"]) for e in entries] == [(200, 1), (500, None)]
