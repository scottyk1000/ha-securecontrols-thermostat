"""Serve the schedule card and load it into Home Assistant's dashboards automatically."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = "securecontrols-schedule-card.js"
URL_BASE = f"/{DOMAIN}"
_REGISTERED = f"{DOMAIN}_card_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card JS and add it to the frontend (once per Home Assistant run)."""
    if hass.data.get(_REGISTERED) or getattr(hass, "http", None) is None:
        return
    path = Path(__file__).parent / CARD_FILE
    version = (await async_get_integration(hass, DOMAIN)).version
    try:
        from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        await hass.http.async_register_static_paths(
            [StaticPathConfig(f"{URL_BASE}/{CARD_FILE}", str(path), cache_headers=False)]
        )
        add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={version}")
    except (ImportError, KeyError, RuntimeError) as err:
        # No frontend (e.g. tests) or already registered: the card is optional.
        _LOGGER.debug("Schedule card not registered: %s", err)
        return
    hass.data[_REGISTERED] = True
