"""Comandi WebSocket lista_spesa/* (docs/PROTOCOLLO.md)."""

from __future__ import annotations

import inspect
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .manager import ListaError, ListaSpesaManager, Subscriber

_LOGGER = logging.getLogger(__name__)

_OPT_STR = vol.Any(None, str)
_OPT_INT = vol.Any(None, vol.Coerce(int))
_OPT_FLOAT = vol.Any(None, vol.Coerce(float))
_OPT_BOOL = vol.Any(None, bool)
_LEVEL = vol.Any(None, vol.In(["info", "warning", "important"]))
_FOR = vol.Any(None, vol.In(["all", "me"]))
_VIS = vol.Any(None, vol.In(["all", "adults"]))

_ITEM_FIELDS = {
    vol.Optional("name"): _OPT_STR,
    vol.Optional("quantity"): _OPT_INT,
    vol.Optional("message"): _OPT_STR,
    vol.Optional("message_level"): _LEVEL,
    vol.Optional("for_whom"): _FOR,
    vol.Optional("visibility"): _VIS,
    vol.Optional("urgent"): _OPT_BOOL,
}

_ITEM_ID = {vol.Required("item_id"): str}
_IN_SESSION = {vol.Required("item_id"): str, vol.Optional("session_id"): _OPT_STR}

# nome comando → (campi, metodo del manager)
COMMANDS: dict[str, tuple[dict, str]] = {
    "state": ({}, "snapshot_cmd"),
    "add": ({vol.Required("name"): str, vol.Optional("id"): _OPT_STR, **{k: v for k, v in _ITEM_FIELDS.items() if k != "name"}}, "add"),
    "update": ({**_ITEM_ID, **_ITEM_FIELDS}, "update"),
    "remove": (_ITEM_ID, "remove"),
    "restore": (_ITEM_ID, "restore"),
    "start_shopping": (
        {
            vol.Optional("store_id"): _OPT_STR,
            vol.Optional("store_name"): _OPT_STR,
            vol.Optional("lat"): _OPT_FLOAT,
            vol.Optional("lon"): _OPT_FLOAT,
            vol.Optional("prices", default=False): bool,
        },
        "start_shopping",
    ),
    "check": (
        {
            **_IN_SESSION,
            vol.Optional("taken", default=0): vol.Coerce(int),
            vol.Optional("close", default=False): bool,
            vol.Optional("price_cents"): _OPT_INT,
        },
        "check",
    ),
    "not_found": (_IN_SESSION, "not_found"),
    "reject": (_IN_SESSION, "reject"),
    "reopen": (_ITEM_ID, "reopen"),
    "set_price": ({**_ITEM_ID, vol.Required("price_cents"): _OPT_INT}, "set_price"),
    "end_shopping": (
        {
            vol.Optional("session_id"): _OPT_STR,
            vol.Optional("silent"): vol.Any(None, dict),
            vol.Optional("actions"): vol.Any(None, [dict]),
            vol.Optional("receipt_total_cents"): _OPT_INT,
            vol.Optional("payer_id"): _OPT_STR,
            vol.Optional("firefly"): vol.Any(None, vol.In(["yes", "no", "always"])),
            vol.Optional("receipt_photo_b64"): _OPT_STR,
        },
        "end_shopping",
    ),
    "do_not_disturb": ({vol.Required("enabled"): bool}, "do_not_disturb"),
    "insist": (_ITEM_ID, "insist"),
    "insist_answer": ({vol.Required("insistence_id"): str, vol.Required("accept"): bool}, "insist_answer"),
    "store_save": (
        {
            vol.Optional("id"): _OPT_STR,
            vol.Optional("name"): _OPT_STR,
            vol.Optional("lat"): _OPT_FLOAT,
            vol.Optional("lon"): _OPT_FLOAT,
        },
        "store_save",
    ),
    "store_delete": ({vol.Required("id"): str}, "store_delete"),
    "history": ({vol.Optional("limit"): _OPT_INT}, "history"),
    "expense": ({vol.Required("expense_id"): str}, "expense"),
    "receipt_match": ({vol.Required("expense_id"): str, vol.Required("lines"): [dict]}, "receipt_match"),
    "receipt_save": (
        {vol.Required("expense_id"): str, vol.Required("lines"): [dict], vol.Optional("photo_b64"): _OPT_STR},
        "receipt_save",
    ),
    "receipt_photo": ({vol.Required("expense_id"): str}, "async_receipt_photo"),
    "balances": ({}, "balances"),
    "settle": (
        {vol.Required("from_id"): str, vol.Required("to_id"): str, vol.Required("amount_cents"): vol.Coerce(int)},
        "settle",
    ),
    "register_device": (
        {vol.Required("token"): str, vol.Optional("platform", default="android"): str, vol.Optional("name", default=""): str},
        "register_device",
    ),
    "unregister_device": ({vol.Required("token"): str}, "unregister_device"),
    "prefs": (
        {
            vol.Optional("shopping_started"): _OPT_BOOL,
            vol.Optional("added_during_shopping"): _OPT_BOOL,
            vol.Optional("firefly_always"): _OPT_BOOL,
        },
        "set_prefs",
    ),
}


def _manager(hass: HomeAssistant) -> ListaSpesaManager | None:
    return hass.data.get(DOMAIN, {}).get("manager")


def _make_handler(name: str, fields: dict, method: str):
    schema = {vol.Required("type"): f"{DOMAIN}/{name}", **fields}

    @websocket_api.websocket_command(schema)
    @websocket_api.async_response
    async def _handler(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        mgr = _manager(hass)
        if mgr is None:
            connection.send_error(msg["id"], "not_loaded", "Integrazione non configurata.")
            return
        user_id = connection.user.id if connection.user else None
        if mgr.role_of(user_id) == "none":
            await mgr.async_refresh_users()
        params = {k: v for k, v in msg.items() if k not in ("id", "type")}
        try:
            if method == "snapshot_cmd":
                result: Any = mgr.snapshot(user_id)
            else:
                result = getattr(mgr, method)(user_id, params)
                if inspect.isawaitable(result):
                    result = await result
            connection.send_result(msg["id"], result)
        except ListaError as err:
            connection.send_error(msg["id"], err.code, err.message)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("Comando %s non valido: %s", name, err)
            connection.send_error(msg["id"], "invalid", str(err))

    return _handler


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@websocket_api.async_response
async def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Iscrizione: evento state subito e a ogni modifica, eventi notice per l'utente."""
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_loaded", "Integrazione non configurata.")
        return
    user_id = connection.user.id if connection.user else None
    await mgr.async_refresh_users()
    try:
        snap = mgr.snapshot(user_id)
    except ListaError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    msg_id = msg["id"]

    def send_state(state: dict) -> None:
        connection.send_message(websocket_api.event_message(msg_id, {"type": "state", "state": state}))

    def send_notice(notice: dict) -> None:
        connection.send_message(websocket_api.event_message(msg_id, {"type": "notice", "notice": notice}))

    connection.subscriptions[msg_id] = mgr.subscribe(Subscriber(user_id, send_state, send_notice))
    connection.send_result(msg_id)
    send_state(snap)


def async_register(hass: HomeAssistant) -> None:
    """Registra tutti i comandi (una volta sola)."""
    if hass.data.setdefault(DOMAIN, {}).get("ws_registered"):
        return
    hass.data[DOMAIN]["ws_registered"] = True
    websocket_api.async_register_command(hass, ws_subscribe)
    for name, (fields, method) in COMMANDS.items():
        websocket_api.async_register_command(hass, _make_handler(name, fields, method))
