"""Set up the real integration in Home Assistant, with only the CSU API mocked."""

from datetime import datetime
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.csu.const import DOMAIN
from custom_components.csu.coordinator import CsuCoordinator
from custom_components.csu.csu import CostRead
from custom_components.csu.csu import CSU
from custom_components.csu.csu import CsuCustomer
from custom_components.csu.csu import Meter
from custom_components.csu.csu import MeterType
from custom_components.csu.csu import ReadResolution
from custom_components.csu.csu import UsageRead
from custom_components.csu.exceptions import CannotConnect
from custom_components.csu.exceptions import InvalidAuth

# Fake values only.
CUSTOMER = CsuCustomer(customer_id="fake-customer", customer_context={"fake": True})
SERVICES = {
    MeterType.ELEC: ("E-NET", ReadResolution.QUARTER_HOUR),
    MeterType.GAS: ("G-TYPICAL", ReadResolution.HOUR),
    MeterType.WATER: ("W-TYPICAL", ReadResolution.DAY),
}


def _meters():
    return [
        Meter(
            customer=CUSTOMER,
            meter_number=n,
            service_number=service,
            service_id=f"fake-service-{n}",
            contract_num="fake-contract",
            contract_account_number="fake-account",
            meter_type=meter_type,
            read_resolution=resolution,
        )
        for n, (meter_type, (service, resolution)) in enumerate(SERVICES.items(), 1)
    ]


async def _login(self):
    self.customers = [CUSTOMER]


async def _get_meters(self):
    self.meters = _meters()


async def _bill_history(self, meter):
    return [
        CostRead(
            meter=meter,
            start_time=datetime(2026, 10, 1),
            end_time=datetime(2026, 10, 31),
            consumption=100.0,
            provided_cost=10.0,
        )
    ]


async def _usage_reads(self, meter, aggregate_type, start, end):
    return [
        UsageRead(
            meter=meter,
            start_time=datetime(2026, 11, 2),
            end_time=datetime(2026, 11, 3),
            consumption=5.0,
            received=1.0,
        )
    ]


@pytest.fixture
def api(recorder_mock):
    """Patch the CSU API methods; yield the login mock so tests can count calls."""
    with (
        patch.object(CSU, "async_login", autospec=True, side_effect=_login) as login,
        patch.object(CSU, "async_get_meters", autospec=True, side_effect=_get_meters),
        patch.object(
            CSU, "async_get_bill_history", autospec=True, side_effect=_bill_history
        ),
        patch.object(
            CSU, "async_get_usage_reads", autospec=True, side_effect=_usage_reads
        ),
        patch.object(CsuCoordinator, "_insert_statistics", autospec=True),
    ):
        yield login


@pytest.fixture
def entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user@example.test", CONF_PASSWORD: "fake-password"},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_loads_entry_and_creates_sensors(hass, entry, api):
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, CsuCoordinator)
    # Sensor setup must reuse the coordinator, not log in again.
    assert api.call_count == 1

    registry = er.async_get(hass)
    for meter_type in SERVICES:
        for kind in ("consumption", "cost"):
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"csu_{meter_type}_{kind}"
            )
            assert entity_id, f"missing csu_{meter_type}_{kind}"
            state = hass.states.get(entity_id)
            assert state is not None
            assert state.state not in ("unavailable", "unknown")


async def test_token_save_does_not_reload_but_option_change_does(hass, entry, api):
    await _setup(hass, entry)
    first = entry.runtime_data

    # The coordinator saves refreshed tokens into entry.data: no reload.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "access_token": "fake-token"}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is first

    hass.config_entries.async_update_entry(entry, options={"electric_rate_per_kwh": 0.1})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not first


async def test_cannot_connect_retries_setup(hass, entry, api):
    api.side_effect = CannotConnect("fake outage")

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_invalid_auth_starts_reauth(hass, entry, api):
    api.side_effect = InvalidAuth("fake rejection")

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)
