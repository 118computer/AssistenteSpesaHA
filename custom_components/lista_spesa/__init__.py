"""Assistente Spesa - HA: lista della spesa di casa per l'app Android e iOS."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, OPT_FCM, OPT_FIREFLY
from .firefly import FireflyClient
from .manager import ListaSpesaManager
from .push import PushSender, parse_service_account
from .websocket import async_register

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.TODO]


def _configure_services(hass: HomeAssistant, mgr: ListaSpesaManager, options: dict[str, Any]) -> None:
    service_account = parse_service_account(options.get(OPT_FCM))
    mgr.push = PushSender(hass, service_account, mgr.remove_device_tokens) if service_account else None
    ff = options.get(OPT_FIREFLY) or {}
    mgr.firefly = (
        FireflyClient(hass, ff["url"], ff["token"], ff.get("account_id")) if ff.get("url") and ff.get("token") else None
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Carica dati, comandi WebSocket ed entità."""
    mgr = ListaSpesaManager(hass, dict(entry.options))
    await mgr.async_load()
    _configure_services(hass, mgr, dict(entry.options))
    hass.data.setdefault(DOMAIN, {})["manager"] = mgr
    async_register(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    mgr: ListaSpesaManager | None = hass.data.get(DOMAIN, {}).get("manager")
    if mgr is None:
        return
    mgr.update_options(dict(entry.options))
    _configure_services(hass, mgr, dict(entry.options))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop("manager", None)
    return unloaded
