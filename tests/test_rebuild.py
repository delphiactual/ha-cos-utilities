"""The one-time hourly history rebuild, against a real recorder and a fake API."""

import logging
import time
from collections import Counter
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.csu.const import CONF_ACCESS_TOKEN
from custom_components.csu.const import CONF_HOURLY_REBUILD_DONE
from custom_components.csu.const import CONF_HOURLY_REBUILD_REFUSED
from custom_components.csu.const import CONF_HOURLY_REBUILD_VERSION
from custom_components.csu.const import DOMAIN
from custom_components.csu.coordinator import CsuCoordinator
from custom_components.csu.coordinator import DAILY_HISTORY
from custom_components.csu.coordinator import HOURLY_EMPTY_WINDOWS
from custom_components.csu.coordinator import HOURLY_WINDOW_DAYS
from custom_components.csu.coordinator import REBUILD_MAX_TAIL
from custom_components.csu.coordinator import REBUILD_MAX_REFUSALS
from custom_components.csu.csu import AggregateType
from custom_components.csu.csu import CostRead
from custom_components.csu.csu import CSU
from custom_components.csu.csu import CsuCustomer
from custom_components.csu.csu import LOCAL_TZ
from custom_components.csu.csu import Meter
from custom_components.csu.csu import MeterType
from custom_components.csu.csu import ReadResolution
from custom_components.csu.csu import UsageRead
from custom_components.csu.exceptions import CannotConnect

UTC = timezone.utc
HOUR = timedelta(hours=1)
# Fake values only. The id has no hyphen because statistic ids only allow [a-z0-9_].
CUSTOMER = CsuCustomer(customer_id="fakecust1", customer_context={"fake": True})
IMPORT_ID = f"{DOMAIN}:csu_elec_fakecust1_energy_consumption"
EXPORT_ID = f"{DOMAIN}:csu_elec_fakecust1_energy_production"
GAS_ID = f"{DOMAIN}:csu_gas_fakecust1_gas_consumption"
SERIES = {
    IMPORT_ID: ("consumption", "consumption"),
    EXPORT_ID: ("received", "returned to grid"),
}
RENEWED_TOKEN = "fake-renewed-token"
# Gas is only read in these hours of each day; CSU reports no usage in the others.
GAS_HOURS = (0, 6, 7, 8, 9)
GAS_PER_HOUR = 1.0
GAS_DAILY_TOTAL = 10.0  # larger than the hourly sum, to prove gas is not filled


def _meter(number, meter_type, resolution):
    return Meter(
        customer=CUSTOMER,
        meter_number=f"fake-meter-{number}",
        service_number="fake-service",
        service_id=f"fake-service-{number}",
        contract_num="fake-contract",
        contract_account_number="fake-account",
        meter_type=meter_type,
        read_resolution=resolution,
    )


def _truth(attr, i):
    """What was really used in hour i; the daily totals include all of it."""
    if attr == "consumption":
        return ((i * 7) % 11) / 4 + 0.1
    return ((i * 5) % 7) / 10


def _has_read(i):
    """CSU's interval data has holes: some hours, and all of day 3, are null."""
    return i % 13 != 5 and i // 24 != 3


class FakeCsu:
    """Fake CSU history: 40 days of electric and gas ending at midnight UTC."""

    def __init__(self):
        today = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        self.base = today - timedelta(days=40)
        self.hours = 40 * 24
        self.fail_deep = False
        self.deep_calls = Counter()
        self.deep_windows = []
        self.deep_ends = []
        self.deep_empty = []
        # Hours read so far on a last day whose daily total is still partial.
        self.partial = 0
        # Days that have a gas read in hour 0; None means every day does.
        self.gas_midnight_days = None
        # Gas is posted late: no reads in its last gas_lag hours.
        self.gas_lag = 0
        # Fake days whose hourly window comes back empty in a regular update
        # (a 500 reads as an empty window).
        self.hole_days = set()
        self.water_hour_calls = 0
        self.water_day_spans = []
        self.elec = _meter(1, MeterType.ELEC, ReadResolution.QUARTER_HOUR)
        self.water = _meter(2, MeterType.WATER, ReadResolution.DAY)
        self.gas = _meter(3, MeterType.GAS, ReadResolution.HOUR)

    def hourly(self):
        reads = []
        for i in range(self.hours + self.partial):
            if not _has_read(i):
                continue
            start = self.base + i * HOUR
            reads.append(
                UsageRead(
                    meter=self.elec,
                    start_time=start,
                    end_time=start + HOUR,
                    consumption=_truth("consumption", i),
                    received=_truth("received", i),
                )
            )
        return reads

    def daily(self):
        reads = []
        for day in range(self.hours // 24):
            start = self.base + timedelta(days=day)
            hours = range(day * 24, day * 24 + 24)
            reads.append(
                UsageRead(
                    meter=self.elec,
                    start_time=start,
                    end_time=start + timedelta(days=1),
                    consumption=sum(_truth("consumption", i) for i in hours),
                    received=sum(_truth("received", i) for i in hours),
                )
            )
        if self.partial:
            start = self.base + timedelta(days=self.hours // 24)
            hours = range(self.hours, self.hours + self.partial)
            reads.append(
                UsageRead(
                    meter=self.elec,
                    start_time=start,
                    end_time=start + timedelta(days=1),
                    # More than the hours read so far, so filling would show.
                    consumption=sum(_truth("consumption", i) for i in hours) + 5.0,
                    received=sum(_truth("received", i) for i in hours) + 1.0,
                )
            )
        return reads

    def gas_has_read(self, i):
        """Real gas has reads in a few hours only, and hour 0 on some days only."""
        hour = i % 24
        if hour not in GAS_HOURS:
            return False
        return (
            hour != 0
            or self.gas_midnight_days is None
            or i // 24 in self.gas_midnight_days
        )

    def gas_hourly(self):
        return [
            UsageRead(
                meter=self.gas,
                start_time=self.base + i * HOUR,
                end_time=self.base + (i + 1) * HOUR,
                consumption=GAS_PER_HOUR,
            )
            for i in range(self.hours - self.gas_lag)
            if self.gas_has_read(i)
        ]

    def gas_daily(self):
        return [
            UsageRead(
                meter=self.gas,
                start_time=self.base + timedelta(days=day),
                end_time=self.base + timedelta(days=day + 1),
                consumption=GAS_DAILY_TOTAL,
            )
            for day in range(self.hours // 24)
        ]

    def usage_reads(self, api, meter, aggregate_type, start, end, **kwargs):
        hourly = aggregate_type == AggregateType.HOUR
        if meter.meter_type == MeterType.WATER:
            if hourly:
                self.water_hour_calls += 1
            else:
                self.water_day_spans.append(end - start)
            return [
                UsageRead(
                    meter=meter,
                    start_time=self.base + timedelta(days=n),
                    end_time=self.base + timedelta(days=n + 1),
                    consumption=1.0,
                    received=0.0,
                )
                for n in range(10)
            ]
        if meter.meter_type == MeterType.GAS:
            reads = self.gas_hourly() if hourly else self.gas_daily()
        else:
            reads = self.hourly() if hourly else self.daily()
        if hourly and not kwargs.get("strict"):
            reads = [
                r
                for r in reads
                if (r.start_time - self.base).days not in self.hole_days
            ]
        if kwargs.get("strict"):
            # A full-history fetch ignores the range, like the real walk does.
            if hourly:
                self.deep_calls[meter.meter_type] += 1
                self.deep_windows.append(kwargs.get("window_days"))
                self.deep_ends.append(end)
                self.deep_empty.append(kwargs.get("stop_after_empty"))
                if self.fail_deep:
                    raise CannotConnect("fake outage")
            return reads
        # The real endpoint works in whole days of the requested time zone.
        zone = start.tzinfo
        first, last = start.date(), end.date()
        return [
            r for r in reads if first <= r.start_time.astimezone(zone).date() <= last
        ]


@pytest.fixture
def fake():
    return FakeCsu()


@pytest.fixture
def patched_api(fake):
    async def login(self):
        self.customers = [CUSTOMER]

    async def meters(self):
        self.meters = [fake.elec, fake.gas, fake.water]

    async def bills(self, meter):
        return [
            CostRead(
                meter=meter,
                start_time=datetime(2026, 10, 1),
                end_time=datetime(2026, 10, 31),
                consumption=100.0,
                provided_cost=10.0,
            )
        ]

    async def ensure_token(self, min_valid):
        self.access_token = RENEWED_TOKEN
        self.token_expires_at = time.time() + 3600
        return True

    with (
        # The fake's days are UTC days. The real window starts at a local midnight,
        # which is also where real days start, so run the integration in UTC here.
        patch("custom_components.csu.coordinator.TIME_ZONE", "UTC"),
        patch.object(CSU, "async_login", autospec=True, side_effect=login),
        patch.object(CSU, "async_get_meters", autospec=True, side_effect=meters),
        patch.object(CSU, "async_get_bill_history", autospec=True, side_effect=bills),
        patch.object(
            CSU, "async_get_usage_reads", autospec=True, side_effect=fake.usage_reads
        ),
        patch.object(
            CSU, "async_ensure_token", autospec=True, side_effect=ensure_token
        ),
    ):
        yield


@pytest.fixture
def entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user@example.test", CONF_PASSWORD: "fake-password"},
    )
    entry.add_to_hass(hass)
    return entry


async def _seed_daily_spikes(hass, fake):
    """Old-style rows: one midnight row per day with the whole day's total."""
    for statistic_id, (attr, label) in SERIES.items():
        running, points = 1000.0, []  # offset so a surviving old row is obvious
        for read in fake.daily():
            value = getattr(read, attr)
            running += value
            points.append(
                StatisticData(start=read.start_time, state=value, sum=running)
            )
        metadata = CsuCoordinator._statistic_metadata(fake.elec, statistic_id, label)
        async_add_external_statistics(hass, metadata, points)
    await get_instance(hass).async_block_till_done()


async def _rows(hass, statistic_id):
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(0, tz=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum", "state"},
    )
    return stats.get(statistic_id, [])


async def _setup(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    await coordinator._stats_task
    await get_instance(hass).async_block_till_done()
    return coordinator


async def _restart(hass, coordinator):
    """What the next Home Assistant start does: a fresh coordinator, same entry."""
    coordinator._rebuild_attempted.clear()
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()


def _by_hour(fake, rows):
    return {round((r["start"] - fake.base.timestamp()) / 3600): r for r in rows}


def _check_history(fake, rows, attr):
    """Every hour has a row, the chain adds up, and days match the daily totals."""
    by_hour = _by_hour(fake, rows)
    assert sorted(by_hour) == list(range(fake.hours))
    running = 0.0
    for i in range(fake.hours):
        running += by_hour[i]["state"]
        assert by_hour[i]["sum"] == pytest.approx(running)
    for day, daily in enumerate(fake.daily()):
        total = sum(by_hour[i]["state"] for i in range(day * 24, day * 24 + 24))
        assert total == pytest.approx(getattr(daily, attr)), f"day {day}"
    if attr == "consumption":
        # Real reads are kept as they are; only the holes are filled.
        for i in range(fake.hours):
            if _has_read(i):
                assert by_hour[i]["state"] == pytest.approx(_truth(attr, i))
    return by_hour


async def test_rebuild_replaces_daily_rows_and_live_updates_continue(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    await _seed_daily_spikes(hass, fake)

    coordinator = await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.data[CONF_HOURLY_REBUILD_DONE]) == {
        IMPORT_ID,
        EXPORT_ID,
        GAS_ID,
    }
    rebuilt = {}
    for statistic_id, (attr, _) in SERIES.items():
        rebuilt[statistic_id] = _check_history(
            fake, await _rows(hass, statistic_id), attr
        )

    # A later poll adds a day of reads; history must not move and the sums must
    # carry on from the rebuilt chain without a jump.
    before_hours = fake.hours
    fake.hours += 24
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()

    assert fake.deep_calls[MeterType.ELEC] == 1  # not rebuilt again
    for statistic_id, (attr, _) in SERIES.items():
        by_hour = _check_history(fake, await _rows(hass, statistic_id), attr)
        for i in range(before_hours):
            assert by_hour[i]["sum"] == pytest.approx(rebuilt[statistic_id][i]["sum"])


async def test_gas_is_used_as_reported_not_filled_from_the_daily_total(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    await _setup(hass, entry)

    rows = await _rows(hass, GAS_ID)
    days = fake.hours // 24
    # Only the real hourly reads are there. Filling would have given 10 a day.
    assert sum(r["state"] for r in rows) == pytest.approx(
        days * len(GAS_HOURS) * GAS_PER_HOUR
    )
    assert rows[-1]["sum"] == pytest.approx(days * len(GAS_HOURS) * GAS_PER_HOUR)
    assert days * GAS_DAILY_TOTAL > rows[-1]["sum"]


async def test_failed_rebuild_changes_nothing_and_does_not_reauth(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    fake.fail_deep = True
    await _seed_daily_spikes(hass, fake)
    seeded = await _rows(hass, IMPORT_ID)

    coordinator = await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert not entry.data.get(CONF_HOURLY_REBUILD_DONE)
    assert not entry.data.get(CONF_HOURLY_REBUILD_REFUSED)  # outages are not refusals
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    # Old rows well before the incremental window are untouched.
    rows = await _rows(hass, IMPORT_ID)
    assert rows[0]["sum"] == pytest.approx(seeded[0]["sum"])

    # It is retried on the next start, not on every poll.
    await coordinator._insert_statistics()
    assert fake.deep_calls[MeterType.ELEC] == 1


async def test_renewed_token_is_saved_for_the_next_start(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    await _setup(hass, entry)

    assert entry.data[CONF_ACCESS_TOKEN] == RENEWED_TOKEN


async def test_walk_that_stops_early_is_logged_and_given_up_after_three_starts(
    recorder_mock,
    enable_custom_integrations,
    hass,
    entry,
    fake,
    patched_api,
    caplog,
):
    # Old rows from before the history the walk can reach: rebuilding would leave
    # them behind with stale sums, so the rebuild must refuse.
    older = fake.base - timedelta(days=10)
    points = [
        StatisticData(start=older + timedelta(days=n), state=1.0, sum=float(n + 1))
        for n in range(50)  # carries on into the period the fake history covers
    ]
    metadata = CsuCoordinator._statistic_metadata(fake.elec, IMPORT_ID, "consumption")
    async_add_external_statistics(hass, metadata, points)
    await get_instance(hass).async_block_till_done()

    with caplog.at_level(logging.INFO):
        coordinator = await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_HOURLY_REBUILD_REFUSED] == {IMPORT_ID: 1}
    assert IMPORT_ID not in entry.data[CONF_HOURLY_REBUILD_DONE]
    # The log names where the walk started and the old row it would have left.
    # Local time, not UTC, so the dates match what the user sees.
    assert fake.base.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") in caplog.text
    assert older.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") in caplog.text
    # The old rows were not touched.
    assert len(await _rows(hass, IMPORT_ID)) >= 3
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    for _ in range(REBUILD_MAX_REFUSALS - 1):
        await _restart(hass, coordinator)
    assert entry.data[CONF_HOURLY_REBUILD_REFUSED][IMPORT_ID] == REBUILD_MAX_REFUSALS
    fetches = fake.deep_calls[MeterType.ELEC]

    # From now on this series is skipped without any request.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _restart(hass, coordinator)
    assert fake.deep_calls[MeterType.ELEC] == fetches
    assert "Not retrying the history rebuild" in caplog.text
    assert entry.state is ConfigEntryState.LOADED


async def test_water_is_not_rebuilt_and_daily_history_is_two_years(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    await _setup(hass, entry)

    assert fake.water_hour_calls == 0
    assert DAILY_HISTORY == timedelta(days=730)
    # The first import asks for exactly two years, not three.
    assert DAILY_HISTORY in fake.water_day_spans
    assert all(span <= DAILY_HISTORY for span in fake.water_day_spans)


async def test_walk_uses_short_windows_and_reaches_tomorrow(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    await _setup(hass, entry)

    # Long ranges come back from CSU with missing reads and a stale tail.
    assert HOURLY_WINDOW_DAYS <= 14
    assert set(fake.deep_windows) == {HOURLY_WINDOW_DAYS}
    # One empty week must not end the history.
    assert set(fake.deep_empty) == {HOURLY_EMPTY_WINDOWS}
    assert HOURLY_EMPTY_WINDOWS > 1
    ahead = fake.deep_ends[0] - dt_util.now()
    assert timedelta(hours=23) < ahead < timedelta(hours=25)


def _entry(hass, **extra):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "user@example.test",
            CONF_PASSWORD: "fake-password",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _seed_hourly(hass, fake, statistic_id, hours, state=0.2):
    """Old rows in hourly form, e.g. written by live updates."""
    start = fake.base
    points, running = [], 1000.0
    for i in hours:
        running += state
        points.append(StatisticData(start=start + i * HOUR, state=state, sum=running))
    metadata = CsuCoordinator._statistic_metadata(
        fake.elec, statistic_id, SERIES[statistic_id][1]
    )
    async_add_external_statistics(hass, metadata, points)
    await get_instance(hass).async_block_till_done()


def _check_chain(rows):
    """Each row's sum is the previous sum plus its own state."""
    for previous, row in zip(rows, rows[1:]):
        assert row["sum"] == pytest.approx(previous["sum"] + row["state"])


async def test_rows_past_the_walk_are_carried_into_the_rebuild(
    recorder_mock, enable_custom_integrations, hass, fake, patched_api, caplog
):
    # Live updates saw 5 hours the walk did not (data lag).
    await _seed_hourly(hass, fake, IMPORT_ID, range(fake.hours + 5))
    entry = _entry(hass)

    with caplog.at_level(logging.INFO):
        coordinator = await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert IMPORT_ID in entry.data[CONF_HOURLY_REBUILD_DONE]
    assert not entry.data.get(CONF_HOURLY_REBUILD_REFUSED)
    assert "carrying 5 existing rows" in caplog.text
    rows = await _rows(hass, IMPORT_ID)
    assert len(rows) == fake.hours + 5
    # The walk itself is complete and exact; the tail keeps its own states and
    # carries on from the rebuilt chain, so nothing keeps an old sum.
    _check_history(fake, rows[: fake.hours], "consumption")
    assert [r["state"] for r in rows[fake.hours :]] == pytest.approx([0.2] * 5)
    _check_chain(rows)
    rebuilt = [r["sum"] for r in rows[: fake.hours]]

    # The next live update replaces the tail with real reads, from the same chain.
    fake.hours += 24
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()

    rows = await _rows(hass, IMPORT_ID)
    _check_history(fake, rows, "consumption")
    assert [r["sum"] for r in rows[: len(rebuilt)]] == pytest.approx(rebuilt)


async def test_rows_far_past_the_walk_are_refused(
    recorder_mock, enable_custom_integrations, hass, fake, patched_api, caplog
):
    far = fake.hours + int(REBUILD_MAX_TAIL.total_seconds() // 3600) + 12
    await _seed_hourly(hass, fake, IMPORT_ID, [fake.hours + 1, far])
    entry = _entry(hass)

    with caplog.at_level(logging.WARNING):
        await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_HOURLY_REBUILD_REFUSED] == {IMPORT_ID: 1}
    assert IMPORT_ID not in entry.data[CONF_HOURLY_REBUILD_DONE]
    assert "Not rebuilding" in caplog.text


async def test_a_new_release_gets_fresh_rebuild_tries(
    recorder_mock, enable_custom_integrations, hass, fake, patched_api
):
    entry = _entry(
        hass,
        **{
            CONF_HOURLY_REBUILD_VERSION: "0.0.1",
            CONF_HOURLY_REBUILD_REFUSED: {IMPORT_ID: 3},
        },
    )

    await _setup(hass, entry)

    version = str((await async_get_integration(hass, DOMAIN)).version)
    assert entry.data[CONF_HOURLY_REBUILD_VERSION] == version
    assert CONF_HOURLY_REBUILD_REFUSED not in entry.data
    assert IMPORT_ID in entry.data[CONF_HOURLY_REBUILD_DONE]
    assert fake.deep_calls[MeterType.ELEC] == 1


@pytest.mark.parametrize("stored", [None, "0.3.0", "0.3.2"])
async def test_series_rebuilt_by_a_release_before_0_3_3_are_rebuilt_again(
    recorder_mock, enable_custom_integrations, hass, fake, patched_api, stored
):
    # No stored version means the flags came from 0.3.0 or 0.3.1. Every release
    # before 0.3.3 left a broken sum chain from its regular update.
    extra = {CONF_HOURLY_REBUILD_DONE: [IMPORT_ID, EXPORT_ID, GAS_ID]}
    if stored:
        extra[CONF_HOURLY_REBUILD_VERSION] = stored
    entry = _entry(hass, **extra)

    await _setup(hass, entry)

    assert fake.deep_calls[MeterType.ELEC] == 1
    assert fake.deep_calls[MeterType.GAS] == 1
    assert set(entry.data[CONF_HOURLY_REBUILD_DONE]) == {IMPORT_ID, EXPORT_ID, GAS_ID}


async def test_series_rebuilt_by_this_release_are_left_alone(
    recorder_mock, enable_custom_integrations, hass, fake, patched_api
):
    version = str((await async_get_integration(hass, DOMAIN)).version)
    entry = _entry(
        hass,
        **{
            CONF_HOURLY_REBUILD_VERSION: version,
            CONF_HOURLY_REBUILD_DONE: [IMPORT_ID, EXPORT_ID, GAS_ID],
        },
    )

    await _setup(hass, entry)

    assert fake.deep_calls[MeterType.ELEC] == 0
    assert fake.deep_calls[MeterType.GAS] == 0


async def test_rebuild_never_fills_hours_that_have_not_happened(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    # The last day has reads through 05:00 and a daily total that is still partial.
    fake.partial = 6

    coordinator = await _setup(hass, entry)

    assert IMPORT_ID in entry.data[CONF_HOURLY_REBUILD_DONE]
    last = fake.hours + fake.partial - 1
    rows = await _rows(hass, IMPORT_ID)
    by_hour = _by_hour(fake, rows)
    assert max(by_hour) == last  # nothing after 05:00
    for i in range(fake.hours, last + 1):
        expected = _truth("consumption", i) if _has_read(i) else 0.0
        assert by_hour[i]["state"] == pytest.approx(expected)
    _check_chain(rows)
    rebuilt = [r["sum"] for r in rows]

    # The next poll sees more hours of that day; they must be written, not skipped.
    fake.partial = 12
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()

    rows = await _rows(hass, IMPORT_ID)
    by_hour = _by_hour(fake, rows)
    assert max(by_hour) == fake.hours + 11
    for i in range(fake.hours + 6, fake.hours + 12):
        if _has_read(i):
            assert by_hour[i]["state"] == pytest.approx(_truth("consumption", i))
    assert [r["sum"] for r in rows[: len(rebuilt)]] == pytest.approx(rebuilt)
    _check_chain(rows)


def _check_gas_chain(fake, rows):
    """Dense hourly rows, each with its own read (never a day's total), no jumps."""
    by_hour = _by_hour(fake, rows)
    first, last = min(by_hour), max(by_hour)
    assert sorted(by_hour) == list(range(first, last + 1))
    for i in range(first, last + 1):
        expected = GAS_PER_HOUR if fake.gas_has_read(i) else 0.0
        assert by_hour[i]["state"] == pytest.approx(expected), f"hour {i}"
        if i > first:
            delta = by_hour[i]["sum"] - by_hour[i - 1]["sum"]
            assert delta >= 0, f"hour {i} goes backwards"
            assert delta == pytest.approx(by_hour[i]["state"]), f"hour {i}"


async def test_the_regular_update_keeps_the_gas_chain_unbroken(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    # Real gas has a read in a few hours only, and in hour 0 on some days only, so
    # the first days of the update window have no read where the daily row starts.
    fake.gas_midnight_days = {20, 33}

    coordinator = await _setup(hass, entry)

    rebuilt = await _rows(hass, GAS_ID)
    _check_gas_chain(fake, rebuilt)
    for _ in range(2):  # the same window rewritten again changes nothing
        await coordinator._insert_statistics()
        await get_instance(hass).async_block_till_done()
        rows = await _rows(hass, GAS_ID)
        _check_gas_chain(fake, rows)
        assert [r["sum"] for r in rows] == pytest.approx([r["sum"] for r in rebuilt])

    # A new day of reads carries on from the same chain.
    fake.hours += 24
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()

    rows = await _rows(hass, GAS_ID)
    _check_gas_chain(fake, rows)
    new_reads = sum(fake.gas_has_read(i) for i in range(fake.hours - 24, fake.hours))
    assert rows[-1]["sum"] == pytest.approx(
        rebuilt[-1]["sum"] + new_reads * GAS_PER_HOUR
    )


async def test_a_failed_window_in_the_middle_keeps_its_hours_and_the_chain(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    coordinator = await _setup(hass, entry)
    rebuilt = await _rows(hass, IMPORT_ID)
    rebuilt_gas = await _rows(hass, GAS_ID)

    fake.hole_days = {fake.hours // 24 - 10}  # inside the update window
    for _ in range(2):
        await coordinator._insert_statistics()
        await get_instance(hass).async_block_till_done()

        rows = await _rows(hass, IMPORT_ID)
        # Hours of the skipped day keep their stored state, and the sums add up
        # across both of its edges.
        _check_history(fake, rows, "state")
        assert [r["state"] for r in rows] == pytest.approx(
            [r["state"] for r in rebuilt]
        )
        assert [r["sum"] for r in rows] == pytest.approx([r["sum"] for r in rebuilt])
        gas = await _rows(hass, GAS_ID)
        assert [r["sum"] for r in gas] == pytest.approx([r["sum"] for r in rebuilt_gas])


async def test_gas_that_is_not_posted_yet_still_updates_what_is(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    fake.gas_lag = 30  # the last day and a bit has no gas reads yet
    coordinator = await _setup(hass, entry)
    _check_gas_chain(fake, await _rows(hass, GAS_ID))

    fake.gas_lag = 6  # most of it is posted by the next update
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()

    rows = await _rows(hass, GAS_ID)
    _check_gas_chain(fake, rows)
    assert max(_by_hour(fake, rows)) >= fake.hours - 6 - 24

    fake.gas_lag = 30  # a later update that sees less must not zero or move anything
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()
    _check_gas_chain(fake, await _rows(hass, GAS_ID))
