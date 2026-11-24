"""The integration's own code on real CSU responses, recorded 2026-11-24 morning.

The fixtures are scrubbed (tests/fixtures). Nothing here touches the network.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from fixture_csu import FixtureSession


async def _api(mod, kind):
    api = mod.CSU(FixtureSession(), "user", "pass")
    await api.async_login()
    await api.async_get_meters()
    return api, next(m for m in api.meters if m.meter_type.name == kind)


async def _hourly(mod, kind):
    api, meter = await _api(mod, kind)
    reads = await api.async_get_usage_reads(
        meter,
        mod.csu.AggregateType.HOUR,
        datetime(2026, 11, 18),
        datetime(2026, 11, 25),
        window_days=7,
        strict=True,
    )
    return meter, reads


async def test_the_daily_endpoint_has_no_read_for_today(mod):
    # CSU answers with a row for today, but with no values in it yet.
    api, meter = await _api(mod, "ELEC")

    reads = await api.async_get_usage_reads(
        meter,
        mod.csu.AggregateType.DAY,
        datetime(2026, 10, 25),
        datetime(2026, 11, 24),
    )

    assert reads
    assert max(r.start_time for r in reads).date().isoformat() == "2026-11-23"


async def test_hourly_reads_stop_before_the_capture_time(mod):
    for kind in ("ELEC", "GAS"):
        _, reads = await _hourly(mod, kind)
        captured = FixtureSession().captured_at
        assert reads
        assert max(r.start_time for r in reads) < captured


async def test_gas_has_reads_in_some_hours_only_and_the_chain_is_dense(mod):
    meter, reads = await _hourly(mod, "GAS")
    window = datetime(2026, 11, 18, tzinfo=mod.csu.LOCAL_TZ)

    dense = mod.csu.dense_hourly_reads(meter, reads, window)

    assert len(dense) > len(reads)  # the hours without a read were filled
    starts = [r.start_time.astimezone(timezone.utc) for r in dense]
    assert all(b - a == timedelta(hours=1) for a, b in zip(starts, starts[1:]))
    assert sum(r.consumption for r in dense) == pytest.approx(
        sum(r.consumption for r in reads if r.start_time >= window)
    )


async def test_the_sum_chain_over_real_gas_has_no_jumps(mod):
    meter, reads = await _hourly(mod, "GAS")
    window = datetime(2026, 11, 18, tzinfo=mod.csu.LOCAL_TZ)
    dense = mod.csu.dense_hourly_reads(meter, reads, window)

    total = 0.0
    chain = []
    for read in dense:
        total += read.consumption
        chain.append((read.consumption, total))

    assert all(state >= 0 for state, _ in chain)
    assert all(
        later[1] - earlier[1] == pytest.approx(later[0])
        for earlier, later in zip(chain, chain[1:])
    )


async def test_reconcile_never_fills_past_the_last_real_electric_read(mod):
    api, meter = await _api(mod, "ELEC")
    hourly = (await _hourly(mod, "ELEC"))[1]
    daily = await api.async_get_usage_reads(
        meter,
        mod.csu.AggregateType.DAY,
        datetime(2026, 10, 25),
        datetime(2026, 11, 24),
    )

    filled = mod.csu.reconcile_hourly_with_daily(hourly, daily)

    assert max(r.start_time for r in filled) == max(r.start_time for r in hourly)


def _local(mod, *args):
    return datetime(*args, tzinfo=mod.csu.LOCAL_TZ)


def _days(reads):
    days = {}
    for r in reads:
        days.setdefault(r.start_time.date().isoformat(), []).append(r)
    return days


async def test_gas_live_update_writes_the_days_that_are_posted(mod):
    # The window that ends at the capture is empty for gas, because the last day
    # is not posted yet. The reads before it still come back.
    api, meter = await _api(mod, "GAS")

    reads = await api.async_get_recent_hourly(
        meter, _local(mod, 2026, 11, 18), _local(mod, 2026, 11, 25)
    )

    days = _days(reads)
    assert sorted(days) == [f"2026-11-{d}" for d in range(18, 23)]
    # Every hour is filled, up to the last real read (labelled 23:00, so 22:00).
    assert [len(days[d]) for d in sorted(days)] == [24, 24, 24, 24, 23]


async def test_electric_with_empty_windows_in_the_middle_keeps_both_sides(mod):
    # Windows nobody recorded answer "no data", like a failed or empty week.
    api = mod.CSU(FixtureSession(empty_outside=True), "user", "pass")
    await api.async_login()
    await api.async_get_meters()
    meter = next(m for m in api.meters if m.meter_type.name == "ELEC")

    reads = await api.async_get_recent_hourly(
        meter, _local(mod, 2026, 10, 25), _local(mod, 2026, 11, 25)
    )

    days = _days(reads)
    # The week ending 11-04 includes all of 11-04.
    kept = {f"2026-10-{d}" for d in range(28, 32)} | {
        f"2026-11-{d:02d}" for d in (1, 2, 3, 4)
    }
    recent = {f"2026-11-{d}" for d in range(18, 24)}
    assert set(days) == kept | recent  # no rows for the empty weeks between
    for day in kept | recent:
        expected = 25 if day == "2026-11-01" else 24
        if day != "2026-11-23":  # the last day stops at the last real read
            assert len(days[day]) == expected, day
