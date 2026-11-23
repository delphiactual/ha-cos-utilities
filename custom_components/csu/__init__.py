"""The Colorado Springs Utilities integration."""

from __future__ import annotations

from homeassistant.const import Platform  # type: ignore
from homeassistant.core import HomeAssistant  # type: ignore

from .coordinator import CsuConfigEntry
from .coordinator import CsuCoordinator

# For your initial PR, limit it to 1 platform.
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: CsuConfigEntry) -> bool:
    """Set up Colorado Springs Utilities from a config entry."""

    coordinator = CsuCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CsuConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def update_listener(hass: HomeAssistant, entry: CsuConfigEntry) -> None:
    """Reload only when options changed, not when the saved token updates."""
    if dict(entry.options) != entry.runtime_data.options:
        hass.config_entries.async_schedule_reload(entry.entry_id)
