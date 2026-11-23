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
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.csu.const import CONF_ACCESS_TOKEN
from custom_components.csu.const import CONF_HOURLY_REBUILD_DONE
from custom_components.csu.const import CONF_HOURLY_REBUILD_REFUSED
from custom_components.csu.const import DOMAIN
from custom_components.csu.coordinator import CsuCoordinator
from custom_components.csu.coordinator import DAILY_HISTORY
from custom_components.csu.coordinator import REBUILD_MAX_REFUSALS
from custom_components.csu.csu import AggregateType
from custom_components.csu.csu import CostRead
from custom_components.csu.csu import CSU
from custom_components.csu.csu import CsuCustomer
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
        self.water_hour_calls = 0
        self.water_day_spans = []
        self.elec = _meter(1, MeterType.ELEC, ReadResolution.QUARTER_HOUR)
        self.water = _meter(2, MeterType.WATER, ReadResolution.DAY)
        self.gas = _meter(3, MeterType.GAS, ReadResolution.HOUR)

    def hourly(self):
        reads = []
        for i in range(self.hours):
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
        return reads

    def gas_hourly(self):
        return [
            UsageRead(
                meter=self.gas,
                start_time=self.base + timedelta(days=day, hours=hour),
                end_time=self.base + timedelta(days=day, hours=hour + 1),
                consumption=GAS_PER_HOUR,
            )
            for day in range(self.hours // 24)
            for hour in GAS_HOURS
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
        if kwargs.get("strict"):
            # A full-history fetch ignores the range, like the real walk does.
            if hourly:
                self.deep_calls[meter.meter_type] += 1
                if self.fail_deep:
                    raise CannotConnect("fake outage")
            return reads
        # The real endpoint works in whole days.
        first = start.astimezone(UTC).date()
        last = end.astimezone(UTC).date()
        return [r for r in reads if first <= r.start_time.date() <= last]


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
    assert str(fake.base.date()) in caplog.text
    assert str(older.date()) in caplog.text
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
