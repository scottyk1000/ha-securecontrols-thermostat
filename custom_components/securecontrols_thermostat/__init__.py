from __future__ import annotations

from contextlib import suppress

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import InvalidAuth, SecureControlsClient
from .const import CONF_EMAIL, CONF_GATEWAY_GMI, CONF_PASSWORD, DOMAIN, PLATFORMS
from .coordinator import ThermoCoordinator
from .frontend import async_register_card


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Secure Controls Thermostat from a config entry."""
    session = async_get_clientsession(hass)
    client = SecureControlsClient(session)

    email = entry.data[CONF_EMAIL].strip()
    password = entry.data[CONF_PASSWORD]
    gmi = entry.data.get(CONF_GATEWAY_GMI)  # optional for logging/use later

    # Pick the configured gateway when the account has more than one
    client.preferred_gmi = str(gmi) if gmi else None

    # Login first; raise proper error so HA can retry if cloud is down
    try:
        await client.login(email, password)
    except InvalidAuth as err:
        # Authentication failures must not enter Home Assistant's automatic
        # setup retry loop, which could repeatedly compete for the account.
        raise ConfigEntryAuthFailed(f"Login rejected: {err}") from err
    except Exception as err:
        # ConfigEntryNotReady triggers HA to retry setup later
        raise ConfigEntryNotReady(f"Login failed: {err}") from err

    # Create a single shared coordinator; refreshes reuse one persistent WS.
    coordinator = ThermoCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    # Multi-zone programmers: register the programmer itself so each zone device
    # can hang off it (via_device).
    if len((coordinator.data or {}).get("zones") or {}) > 1 and gmi:
        ther = client.thermostat
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, str(gmi))},
            manufacturer="Secure Meters",
            model="Smart programmer",
            name=f"Secure programmer {ther.hn or ther.sn or ''}".strip() if ther else None,
            serial_number=ther.sn if ther else None,
        )

    # Stash objects for platforms
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "gmi": gmi,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _link_zone_devices(hass, entry, str(gmi) if gmi else None)
    await async_register_card(hass)
    return True


def _link_zone_devices(hass: HomeAssistant, entry: ConfigEntry, gmi: str | None) -> None:
    """Show each zone device under the programmer device.

    Done through the registry (via_device_id) rather than DeviceInfo.via_device, which
    newer Home Assistant versions deprecate.
    """
    if not gmi:
        return
    registry = dr.async_get(hass)
    parent = registry.async_get_device(identifiers={(DOMAIN, gmi)})
    if parent is None:
        return
    prefix = f"{gmi}_zone"
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if parent.id in (device.id, device.via_device_id):
            continue
        if any(d == DOMAIN and str(i).startswith(prefix) for d, i in device.identifiers):
            registry.async_update_device(device.id, via_device_id=parent.id)



async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        client: SecureControlsClient | None = data.get("client")
        # Close WebSocket if open
        with suppress(Exception):
            if client is not None:
                await client.disconnect()
    return unload_ok
