"""Sensore: numero di prodotti da comprare (per le automazioni)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATED


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([DaComprareSensor(hass.data[DOMAIN]["manager"])])


class DaComprareSensor(SensorEntity):
    """Prodotti ancora da comprare."""

    _attr_has_entity_name = False
    _attr_name = "Lista spesa: da comprare"
    _attr_unique_id = f"{DOMAIN}_da_comprare"
    _attr_icon = "mdi:cart-outline"
    _attr_native_unit_of_measurement = "prodotti"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(self, manager) -> None:
        self._mgr = manager

    @property
    def native_value(self) -> int:
        return len(self._mgr.open_items())

    @property
    def extra_state_attributes(self) -> dict:
        shoppers = [s.get("store_name") for s in self._mgr.data["sessions"].values()]
        return {"spesa_in_corso": bool(shoppers), "supermercati": shoppers}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
