"""Long history walks: windows, strict errors and token margin (no Home Assistant)."""

import time
from datetime import datetime

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
