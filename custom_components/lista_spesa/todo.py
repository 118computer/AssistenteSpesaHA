"""Entità todo "Lista della spesa": dashboard e Assist ("aggiungi il latte alla lista della spesa").

Non mostra i prodotti "Solo adulti": in HA ogni utente vede tutte le entità.
"""

from __future__ import annotations

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ROLE_NONE, SIGNAL_UPDATED
from .manager import ListaError


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([ListaSpesaTodo(hass.data[DOMAIN]["manager"])])


class ListaSpesaTodo(TodoListEntity):
    """La lista condivisa vista da Home Assistant."""

    _attr_has_entity_name = False
    _attr_name = "Lista della spesa"
    _attr_unique_id = f"{DOMAIN}_lista"
    _attr_icon = "mdi:cart"
    _attr_should_poll = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, manager) -> None:
        self._mgr = manager

    def _user(self) -> str | None:
        ctx = getattr(self, "_context", None)
        user_id = getattr(ctx, "user_id", None) if ctx else None
        if user_id and self._mgr.role_of(user_id) != ROLE_NONE:
            return user_id
        return self._mgr.default_user()

    @property
    def todo_items(self) -> list[TodoItem]:
        result = []
        for item in self._mgr.todo_items():
            if item.get("visibility") == "adults":
                continue
            qty = int(item.get("quantity", 1))
            summary = item["name"] if qty <= 1 else f"{item['name']} ×{qty}"
            result.append(
                TodoItem(
                    summary=summary,
                    uid=item["id"],
                    status=TodoItemStatus.COMPLETED if item["status"] == "taken" else TodoItemStatus.NEEDS_ACTION,
                    description=item.get("message") or None,
                )
            )
        return result

    async def async_create_todo_item(self, item: TodoItem) -> None:
        user = self._user()
        if user is None or not item.summary:
            return
        await self._mgr.async_refresh_users()
        params = {"name": item.summary}
        if item.description:
            params["message"] = item.description
        self._mgr.add(user, params)

    async def async_update_todo_item(self, item: TodoItem) -> None:
        user = self._user()
        if user is None or not item.uid:
            return
        existing = self._mgr.data["items"].get(item.uid)
        if existing is None:
            return
        try:
            if item.status == TodoItemStatus.COMPLETED and existing["status"] in ("open", "not_found"):
                self._mgr.remove(user, {"item_id": item.uid})
                return
            params = {"item_id": item.uid}
            if item.summary and item.summary.split(" ×")[0] != existing["name"]:
                params["name"] = item.summary
            if item.description is not None and item.description != existing.get("message"):
                params["message"] = item.description
            if len(params) > 1:
                self._mgr.update(user, params)
        except ListaError:
            return

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        user = self._user()
        if user is None:
            return
        for uid in uids:
            try:
                self._mgr.remove(user, {"item_id": uid})
            except ListaError:
                continue

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
