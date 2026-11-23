"""The one-time hourly history rebuild, against a real recorder and a fake API."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
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

from custom_components.csu.const import CONF_HOURLY_REBUILD_DONE
from custom_components.csu.const import DOMAIN
from custom_components.csu.coordinator import CsuCoordinator
from custom_components.csu.coordinator import DAILY_HISTORY
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
SERIES = {
    IMPORT_ID: ("consumption", "consumption"),
    EXPORT_ID: ("received", "returned to grid"),
}


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


def _value(attr, i):
    """Hours with i % 13 == 5 have no read at all, like many gas hours."""
    if i % 13 == 5:
        return 0.0
    if attr == "consumption":
        return ((i * 7) % 11) / 4 + 0.1
    return ((i * 5) % 7) / 10


class FakeCsu:
    """Fake CSU history: 40 days of hourly electric reads ending at midnight UTC."""

    def __init__(self):
        today = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        self.base = today - timedelta(days=40)
        self.hours = 40 * 24
        self.fail_deep = False
        self.deep_calls = 0
        self.water_hour_calls = 0
        self.water_day_spans = []
        self.elec = _meter(1, MeterType.ELEC, ReadResolution.QUARTER_HOUR)
        self.water = _meter(2, MeterType.WATER, ReadResolution.DAY)

    def hourly(self):
        reads = []
        for i in range(self.hours):
            if i % 13 == 5:
                continue
            start = self.base + i * HOUR
            reads.append(
                UsageRead(
                    meter=self.elec,
                    start_time=start,
                    end_time=start + HOUR,
                    consumption=_value("consumption", i),
                    received=_value("received", i),
                )
            )
        return reads

    def daily(self):
        days = {}
        for read in self.hourly():
            day = read.start_time.replace(hour=0)
            total = days.setdefault(day, [0.0, 0.0])
            total[0] += read.consumption
            total[1] += read.received
        return [
            UsageRead(
                meter=self.elec,
                start_time=day,
                end_time=day + timedelta(days=1),
                consumption=imp,
                received=exp,
            )
            for day, (imp, exp) in sorted(days.items())
        ]

    def expected(self, attr):
        """(values, sums) per hour from the first hour, missing hours as zero."""
        values = [_value(attr, i) for i in range(self.hours)]
        sums, running = [], 0.0
        for value in values:
            running += value
            sums.append(running)
        return values, sums

    def usage_reads(self, api, meter, aggregate_type, start, end, **kwargs):
        if meter.meter_type == MeterType.WATER:
            if aggregate_type == AggregateType.HOUR:
                self.water_hour_calls += 1
            else:
                self.water_day_spans.append(end - start)
            day = self.base
            return [
                UsageRead(
                    meter=meter,
                    start_time=day + timedelta(days=n),
                    end_time=day + timedelta(days=n + 1),
                    consumption=1.0,
                    received=0.0,
                )
                for n in range(10)
            ]
        if kwargs.get("strict"):
            self.deep_calls += 1
            if self.fail_deep:
                raise CannotConnect("fake outage")
            return self.hourly()
        # The real endpoint works in whole days.
        first = start.astimezone(UTC).date()
        last = end.astimezone(UTC).date()
        reads = self.hourly() if aggregate_type == AggregateType.HOUR else self.daily()
        return [r for r in reads if first <= r.start_time.date() <= last]


@pytest.fixture
def fake():
    return FakeCsu()


@pytest.fixture
def patched_api(fake):
    async def login(self):
        self.customers = [CUSTOMER]

    async def meters(self):
        self.meters = [fake.elec, fake.water]

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

    with (
        patch.object(CSU, "async_login", autospec=True, side_effect=login),
        patch.object(CSU, "async_get_meters", autospec=True, side_effect=meters),
        patch.object(CSU, "async_get_bill_history", autospec=True, side_effect=bills),
        patch.object(
            CSU, "async_get_usage_reads", autospec=True, side_effect=fake.usage_reads
        ),
        patch.object(CSU, "async_ensure_token", new=AsyncMock(return_value=False)),
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


async def test_rebuild_replaces_daily_rows_and_live_updates_continue(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    await _seed_daily_spikes(hass, fake)

    coordinator = await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.data[CONF_HOURLY_REBUILD_DONE]) == {IMPORT_ID, EXPORT_ID}
    for statistic_id, (attr, _) in SERIES.items():
        values, sums = fake.expected(attr)
        rows = await _rows(hass, statistic_id)
        # Every hour has a row (missing reads as zero), so no old daily row survives.
        assert len(rows) == fake.hours
        assert rows[0]["start"] == fake.base.timestamp()
        assert [r["state"] for r in rows] == pytest.approx(values)
        assert [r["sum"] for r in rows] == pytest.approx(sums)

    # A later poll adds a day of reads; sums must carry on from the rebuilt chain.
    fake.hours += 24
    await coordinator._insert_statistics()
    await get_instance(hass).async_block_till_done()

    assert fake.deep_calls == 1  # not rebuilt again
    for statistic_id, (attr, _) in SERIES.items():
        values, sums = fake.expected(attr)
        rows = await _rows(hass, statistic_id)
        assert len(rows) == fake.hours
        assert [r["sum"] for r in rows] == pytest.approx(sums)


async def test_failed_rebuild_changes_nothing_and_does_not_reauth(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    fake.fail_deep = True
    await _seed_daily_spikes(hass, fake)
    seeded = await _rows(hass, IMPORT_ID)

    coordinator = await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert not entry.data.get(CONF_HOURLY_REBUILD_DONE)
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    # Old rows well before the incremental window are untouched.
    rows = await _rows(hass, IMPORT_ID)
    assert rows[0]["sum"] == pytest.approx(seeded[0]["sum"])

    # It is retried on the next start, not on every poll.
    await coordinator._insert_statistics()
    assert fake.deep_calls == 1


async def test_water_is_not_rebuilt_and_daily_history_is_two_years(
    recorder_mock, enable_custom_integrations, hass, entry, fake, patched_api
):
    await _setup(hass, entry)

    assert fake.water_hour_calls == 0
    assert DAILY_HISTORY == timedelta(days=730)
    # The first import asks for exactly two years, not three.
    assert DAILY_HISTORY in fake.water_day_spans
    assert all(span <= DAILY_HISTORY for span in fake.water_day_spans)
