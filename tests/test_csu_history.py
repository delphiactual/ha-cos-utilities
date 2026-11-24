"""Long history walks: windows, strict errors and token margin (no Home Assistant)."""

import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import aiohttp
import pytest

from conftest import NEW_TOKEN
from conftest import SAVED_TOKEN
from conftest import make_api
from conftest import token_ok

HOURLY_URL = "https://myaccount.csu.org/rest/usage/detail/day"


@pytest.fixture
def meter(mod):
    csu = mod.csu
    customer = csu.CsuCustomer(customer_id="fake-customer", customer_context={})
    return csu.Meter(
        customer=customer,
        meter_number="fake-meter",
        service_number="E-NET",
        service_id="fake-service",
        contract_num="fake-contract",
        contract_account_number="fake-account",
        meter_type=csu.MeterType.ELEC,
        read_resolution=csu.ReadResolution.QUARTER_HOUR,
    )


def _rows(day: str):
    return {
        "history": [
            {
                "usageDate": f"{day} 01:00",
                "usageConsumptionValue": "1.5",
                "netReceivedValue": None,
                "intervalType": 60,
            }
        ]
    }


async def _hourly(api, mod, meter, **kwargs):
    return await api.async_get_usage_reads(
        meter,
        mod.csu.AggregateType.HOUR,
        datetime(2026, 1, 1),
        datetime(2026, 4, 11),
        **kwargs,
    )


async def test_walk_uses_the_window_and_stops_at_the_first_empty_one(
    mod, session, meter
):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    session.reply(HOURLY_URL, payload=_rows("2026-04-01"))
    session.reply(HOURLY_URL, payload=_rows("2026-03-01"))
    session.reply(HOURLY_URL, payload={"history": []})

    reads = await _hourly(api, mod, meter, window_days=30)

    assert len(reads) == 2
    assert session.calls[HOURLY_URL] == 3  # never asks past the empty window
    spans = [(b["fromDate"][:10], b["toDate"][:10]) for b in session.bodies[HOURLY_URL]]
    assert spans[0] == ("2026-03-12", "2026-04-11")
    assert spans[1] == ("2026-02-10", "2026-03-12")


async def test_strict_retries_then_raises_instead_of_looking_like_the_end(
    mod, session, meter
):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    for _ in range(3):
        session.reply(HOURLY_URL, status=500, payload={})

    with pytest.raises(mod.errors.CannotConnect):
        await _hourly(api, mod, meter, window_days=30, strict=True, retry_delays=(0, 0))

    assert session.calls[HOURLY_URL] == 3


async def test_strict_recovers_when_a_retry_succeeds(mod, session, meter):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    session.fail(HOURLY_URL, aiohttp.ClientConnectionError("fake drop"))
    session.reply(HOURLY_URL, payload=_rows("2026-04-01"))
    session.reply(HOURLY_URL, payload={"history": []})

    reads = await _hourly(
        api, mod, meter, window_days=30, strict=True, retry_delays=(0,)
    )

    assert len(reads) == 1


async def test_strict_rejected_token_is_not_retried(mod, session, meter):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    session.reply(HOURLY_URL, status=401, payload={})

    with pytest.raises(mod.errors.InvalidAuth):
        await _hourly(api, mod, meter, window_days=30, strict=True, retry_delays=(0, 0))

    assert session.calls[HOURLY_URL] == 1


async def test_default_walk_still_treats_a_server_error_as_the_end(mod, session, meter):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    session.fail(
        HOURLY_URL, aiohttp.ClientResponseError(None, (), status=500, message="fake")
    )

    assert await _hourly(api, mod, meter) == []


async def test_ensure_token_only_renews_when_it_would_expire_too_soon(mod, session):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=600)

    assert await api.async_ensure_token(60) is False
    assert session.calls[mod.TOKEN_URL] == 0

    token_ok(session, mod)
    assert await api.async_ensure_token(1800) is True
    assert api.access_token == NEW_TOKEN
    assert api.token_expires_at > time.time() + 3000


def _read(mod, meter, start, imp=0.0, exp=0.0, hours=1):
    return mod.csu.UsageRead(
        meter=meter,
        start_time=start,
        end_time=start + timedelta(hours=hours),
        consumption=imp,
        received=exp,
    )


def _day(mod, y, m, d):
    return datetime(y, m, d, tzinfo=mod.csu.LOCAL_TZ)


def _hours_of(reads, day):
    return [r for r in reads if r.start_time.date() == day.date()]


def test_reconcile_fills_a_day_with_no_interval_data(mod, meter):
    day = _day(mod, 2026, 5, 10)
    before = _day(mod, 2026, 5, 9)
    # Another day shows that export only happens in hours 9 to 15.
    real = [
        _read(
            mod,
            meter,
            before + timedelta(hours=h),
            imp=1.0,
            exp=0.5 if 9 <= h <= 15 else 0.0,
        )
        for h in range(24)
    ]
    # A later real read shows the hourly walk got past the missing day.
    real.append(_read(mod, meter, day + timedelta(days=1), imp=1.0))
    daily = [
        _read(mod, meter, before, imp=24.0, exp=3.5, hours=24),
        _read(mod, meter, day, imp=24.0, exp=2.4, hours=24),
    ]

    out = mod.csu.reconcile_hourly_with_daily(real, daily)

    filled = _hours_of(out, day)
    assert len(filled) == 24
    assert sum(r.consumption for r in filled) == pytest.approx(24.0)
    assert sum(r.received for r in filled) == pytest.approx(2.4)
    # Export lands only in the solar hours, never overnight.
    assert {r.start_time.hour for r in filled if r.received > 0} == set(range(9, 16))


def test_reconcile_skips_export_when_none_was_ever_seen(mod, meter):
    day = _day(mod, 2026, 5, 10)
    daily = [_read(mod, meter, day, imp=24.0, exp=2.4, hours=24)]
    later = [_read(mod, meter, day + timedelta(days=1), imp=1.0)]

    out = _hours_of(mod.csu.reconcile_hourly_with_daily(later, daily), day)

    assert sum(r.consumption for r in out) == pytest.approx(24.0)
    assert sum(r.received for r in out) == 0.0


def test_reconcile_partial_export_day_fills_only_the_missing_hours(mod, meter):
    day = _day(mod, 2026, 5, 10)
    # Hours 12 and 13 have no read. Every other hour has one, including real
    # zero export at night and in the evening.
    present = [h for h in range(24) if h not in (12, 13)]
    real = [
        _read(
            mod,
            meter,
            day + timedelta(hours=h),
            imp=1.0,
            exp=0.5 if 8 <= h <= 17 else 0.0,
        )
        for h in present
    ]
    daily = [_read(mod, meter, day, imp=22.0, exp=5.0 * 1.0 + 1.0, hours=24)]

    out = mod.csu.reconcile_hourly_with_daily(real, daily)

    by_hour = {r.start_time.hour: r for r in out}
    # 8 present solar hours hold 4.0; the daily total is 6.0, so 2.0 is missing.
    assert by_hour[12].received == pytest.approx(1.0)
    assert by_hour[13].received == pytest.approx(1.0)
    for hour in present:
        assert by_hour[hour].received == (0.5 if 8 <= hour <= 17 else 0.0)
    assert sum(r.received for r in out) == pytest.approx(6.0)


def test_reconcile_never_fills_a_present_hour_even_when_its_value_is_zero(mod, meter):
    day = _day(mod, 2026, 5, 10)
    real = [
        _read(mod, meter, day + timedelta(hours=h), imp=0.0 if h == 3 else 1.0)
        for h in range(24)
    ]
    daily = [_read(mod, meter, day, imp=30.0, hours=24)]

    out = mod.csu.reconcile_hourly_with_daily(real, daily)

    # No hour is missing, so the shortfall has nowhere truthful to go.
    assert [r.consumption for r in out] == [0.0 if h == 3 else 1.0 for h in range(24)]


def test_reconcile_only_adds_to_hours_without_data(mod, meter):
    day = _day(mod, 2026, 5, 10)
    real = [_read(mod, meter, day + timedelta(hours=h), imp=1.0) for h in range(10)]
    real.append(_read(mod, meter, day + timedelta(days=1), imp=1.0))
    daily = [_read(mod, meter, day, imp=24.0, hours=24)]

    out = _hours_of(mod.csu.reconcile_hourly_with_daily(real, daily), day)

    assert sum(r.consumption for r in out) == pytest.approx(24.0)
    assert len(out) == 24
    assert [r.consumption for r in out[:10]] == [1.0] * 10  # real reads untouched
    assert [r.consumption for r in out[10:]] == pytest.approx([14 / 14] * 14)


def test_reconcile_leaves_complete_days_and_days_without_a_daily_alone(mod, meter):
    day = _day(mod, 2026, 5, 10)
    other = _day(mod, 2026, 5, 11)
    real = [_read(mod, meter, day + timedelta(hours=h), imp=1.0) for h in range(24)]
    real += [_read(mod, meter, other + timedelta(hours=1), imp=3.0)]
    daily = [_read(mod, meter, day, imp=24.0, hours=24)]  # no daily for `other`

    out = mod.csu.reconcile_hourly_with_daily(real, daily)

    assert [(r.start_time, r.consumption) for r in out] == [
        (r.start_time, r.consumption) for r in real
    ]


def test_reconcile_never_removes_when_hourly_exceeds_daily(mod, meter):
    day = _day(mod, 2026, 5, 10)
    real = [_read(mod, meter, day + timedelta(hours=h), imp=2.0) for h in range(24)]
    daily = [_read(mod, meter, day, imp=40.0, hours=24)]

    out = mod.csu.reconcile_hourly_with_daily(real, daily)

    assert sum(r.consumption for r in out) == pytest.approx(48.0)


@pytest.mark.parametrize(
    "day, hours", [((2026, 3, 8), 23), ((2026, 11, 1), 25), ((2026, 6, 1), 24)]
)
def test_reconcile_handles_dst_days(mod, meter, day, hours):
    start = _day(mod, *day)
    daily = [_read(mod, meter, start, imp=float(hours), hours=hours)]
    later = [_read(mod, meter, start + timedelta(days=2), imp=1.0)]

    out = _hours_of(mod.csu.reconcile_hourly_with_daily(later, daily), start)

    assert len(out) == hours
    assert len({r.start_time.astimezone(timezone.utc) for r in out}) == hours
    assert sum(r.consumption for r in out) == pytest.approx(float(hours))


def test_reconcile_never_fills_today_or_hours_that_have_not_happened(mod, meter):
    yesterday = _day(mod, 2026, 5, 9)
    today = _day(mod, 2026, 5, 10)
    # Yesterday is complete except hour 3; today has reads through 05:00 only.
    real = [
        _read(mod, meter, yesterday + timedelta(hours=h), imp=1.0)
        for h in range(24)
        if h != 3
    ]
    real += [_read(mod, meter, today + timedelta(hours=h), imp=1.0) for h in range(6)]
    daily = [
        _read(mod, meter, yesterday, imp=24.0, hours=24),
        _read(mod, meter, today, imp=10.0, hours=24),  # partial: the day isn't over
    ]

    out = mod.csu.reconcile_hourly_with_daily(real, daily)

    # The earlier, fully covered day still gets its missing hour.
    assert sum(r.consumption for r in _hours_of(out, yesterday)) == pytest.approx(24.0)
    # Today stays exactly as read, and nothing is added after 05:00.
    assert [(r.start_time, r.consumption) for r in _hours_of(out, today)] == [
        (r.start_time, r.consumption) for r in real[-6:]
    ]
    assert max(r.start_time for r in out) == today + timedelta(hours=5)


async def test_walk_continues_past_an_empty_window_when_asked_to(mod, session, meter):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    session.reply(HOURLY_URL, payload=_rows("2026-04-01"))
    session.reply(HOURLY_URL, payload={"history": []})  # a gap in the middle
    session.reply(HOURLY_URL, payload=_rows("2026-02-01"))
    session.reply(HOURLY_URL, payload=_rows("2026-01-05"))

    reads = await _hourly(api, mod, meter, window_days=30, stop_after_empty=2)

    assert len(reads) == 3
    assert session.calls[HOURLY_URL] == 4


async def test_walk_ends_after_that_many_empty_windows_in_a_row(mod, session, meter):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    session.reply(HOURLY_URL, payload=_rows("2026-04-01"))
    session.reply(HOURLY_URL, payload={"history": []})
    session.reply(HOURLY_URL, payload={"history": []})

    reads = await _hourly(api, mod, meter, window_days=30, stop_after_empty=2)

    assert len(reads) == 1
    assert session.calls[HOURLY_URL] == 3  # the fourth window is never requested


async def test_the_empty_count_starts_over_after_a_window_with_reads(
    mod, session, meter
):
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)
    for payload in (
        {"history": []},
        _rows("2026-03-20"),
        {"history": []},
        _rows("2026-02-01"),
        {"history": []},
        {"history": []},
    ):
        session.reply(HOURLY_URL, payload=payload)

    reads = await _hourly(api, mod, meter, window_days=15, stop_after_empty=2)

    # Single empty windows never add up; only two in a row end the walk.
    assert len(reads) == 2
    assert session.calls[HOURLY_URL] == 6


def test_dense_hourly_reads_give_every_hour_a_row_and_stop_at_the_last_read(mod, meter):
    window = _day(mod, 2026, 5, 10)
    reads = [
        _read(mod, meter, window + timedelta(hours=h), imp=1.0, exp=0.5) for h in (3, 5)
    ]

    out = mod.csu.dense_hourly_reads(meter, reads, window)

    # The window starts at 00:00 even though nothing was read until 03:00, and
    # nothing is added after the last read.
    assert [r.start_time.hour for r in out] == [0, 1, 2, 3, 4, 5]
    assert [r.consumption for r in out] == [0.0, 0.0, 0.0, 1.0, 0.0, 1.0]
    assert [r.received for r in out] == [0.0, 0.0, 0.0, 0.5, 0.0, 0.5]
    assert out[3] is reads[0]  # real reads are passed through untouched


def test_dense_hourly_reads_of_nothing_is_nothing(mod, meter):
    assert mod.csu.dense_hourly_reads(meter, [], _day(mod, 2026, 5, 10)) == []


def test_dense_hourly_reads_ignore_reads_before_the_window(mod, meter):
    window = _day(mod, 2026, 5, 10)
    reads = [
        _read(mod, meter, window - timedelta(hours=1), imp=9.0),
        _read(mod, meter, window + timedelta(hours=1), imp=1.0),
    ]

    out = mod.csu.dense_hourly_reads(meter, reads, window)

    assert [(r.start_time.hour, r.consumption) for r in out] == [(0, 0.0), (1, 1.0)]


@pytest.mark.parametrize(
    "day, last_hour, rows",
    [((2026, 3, 8), 22, 22), ((2026, 11, 1), 23, 25), ((2026, 6, 1), 23, 24)],
)
def test_dense_hourly_reads_step_through_dst_changes(mod, meter, day, last_hour, rows):
    window = _day(mod, *day)
    reads = [_read(mod, meter, window + timedelta(hours=last_hour), imp=1.0)]

    out = mod.csu.dense_hourly_reads(meter, reads, window)

    assert len(out) == rows
    assert len({r.start_time.astimezone(timezone.utc) for r in out}) == rows
