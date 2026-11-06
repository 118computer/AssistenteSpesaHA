"""Gestore dei dati: lista, spese, supermercati, insistenze, storico (docs/PROTOCOLLO.md)."""

from __future__ import annotations

import base64
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import logic, texts
from .const import (
    CATALOG_MAX,
    DEFAULT_LANGUAGE,
    DND_NO_SESSION_HOURS,
    DND_SESSION_HOURS,
    MAX_INSIST,
    OPT_FCM,
    OPT_FIREFLY,
    OPT_JUNIOR_ADULT,
    OPT_ROLES,
    RECEIPTS_DIR,
    ROLE_ADULT,
    ROLE_JUNIOR,
    ROLE_NONE,
    SIGNAL_UPDATED,
    STATE_DEBOUNCE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

EDITABLE = ("name", "quantity", "message", "message_level", "for_whom", "visibility", "urgent")


class ListaError(Exception):
    """Errore di un comando: code come da protocollo."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _now() -> str:
    return dt_util.utcnow().isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _is_today(iso: str | None) -> bool:
    if not iso:
        return False
    parsed = dt_util.parse_datetime(iso)
    if parsed is None:
        return False
    return dt_util.as_local(parsed).date() == dt_util.now().date()


def _empty_data() -> dict[str, Any]:
    return {
        "rev": 0,
        "items": {},
        "sessions": {},
        "stores": {},
        "catalog": {},
        "expenses": [],
        "insistences": {},
        "devices": {},
        "prefs": {},
        "dnd": {},
        "settlements": [],
        "receipt_maps": {},
    }


class Subscriber:
    """Una connessione WebSocket iscritta agli aggiornamenti."""

    def __init__(self, user_id: str, send_state: Callable[[dict], None], send_notice: Callable[[dict], None]) -> None:
        self.user_id = user_id
        self.send_state = send_state
        self.send_notice = send_notice


class ListaSpesaManager:
    """Tiene i dati in memoria, li salva nello storage di HA e avvisa gli iscritti."""

    def __init__(self, hass: HomeAssistant, options: dict[str, Any]) -> None:
        self.hass = hass
        self.options: dict[str, Any] = dict(options or {})
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = _empty_data()
        self._subscribers: list[Subscriber] = []
        self._users: list[dict[str, Any]] = []
        self._pending_state: Any = None
        self.push = None  # PushSender, impostato da __init__.py
        self.firefly = None  # FireflyClient, impostato da __init__.py

    # ------------------------------------------------------------------ avvio, salvataggio

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            base = _empty_data()
            base.update(stored)
            self.data = base
        await self.async_refresh_users()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.data, 1.0)

    def update_options(self, options: dict[str, Any]) -> None:
        self.options = dict(options or {})
        self.hass.async_create_task(self._refresh_and_broadcast())

    async def _refresh_and_broadcast(self) -> None:
        await self.async_refresh_users()
        self._changed(save=False)

    # ------------------------------------------------------------------ utenti e ruoli

    async def async_refresh_users(self) -> None:
        users = await self.hass.auth.async_get_users()
        result = []
        for user in users:
            if user.system_generated or not user.is_active:
                continue
            result.append({"id": user.id, "full_name": user.name or "Utente"})
        self._users = result

    @property
    def roles(self) -> dict[str, str]:
        return self.options.get(OPT_ROLES, {}) or {}

    @property
    def junior_adult(self) -> dict[str, str]:
        return self.options.get(OPT_JUNIOR_ADULT, {}) or {}

    def role_of(self, user_id: str | None) -> str:
        if not user_id or user_id not in {u["id"] for u in self._users}:
            return ROLE_NONE
        return self.roles.get(user_id, ROLE_ADULT)

    def active_users(self) -> list[dict[str, Any]]:
        names = logic.display_names(self._users)
        result = []
        for user in self._users:
            role = self.role_of(user["id"])
            if role == ROLE_NONE:
                continue
            result.append(
                {
                    "id": user["id"],
                    "name": names.get(user["id"], user["full_name"]),
                    "full_name": user["full_name"],
                    "role": role,
                    "adult_id": self.junior_adult.get(user["id"]) if role == ROLE_JUNIOR else None,
                }
            )
        return result

    def adult_ids(self) -> list[str]:
        return [u["id"] for u in self.active_users() if u["role"] == ROLE_ADULT]

    def name_of(self, user_id: str | None, lang: str = DEFAULT_LANGUAGE) -> str:
        for user in self.active_users():
            if user["id"] == user_id:
                return user["name"]
        return texts.t(lang, "someone")

    def require_user(self, user_id: str | None) -> str:
        role = self.role_of(user_id)
        if role == ROLE_NONE:
            raise ListaError("no_role", "Il tuo utente non ha un ruolo in Assistente Spesa: chiedi all'amministratore.")
        return role

    def require_adult(self, user_id: str | None) -> None:
        if self.require_user(user_id) != ROLE_ADULT:
            raise ListaError("not_allowed", "Solo gli Adulti possono farlo.")

    # ------------------------------------------------------------------ preferenze

    def prefs_of(self, user_id: str) -> dict[str, Any]:
        prefs = self.data["prefs"].get(user_id, {})
        return {
            "shopping_started": prefs.get("shopping_started", True),
            "added_during_shopping": prefs.get("added_during_shopping", True),
            "firefly_always": prefs.get("firefly_always", False),
        }

    def language_of(self, user_id: str | None) -> str:
        """Lingua delle notifiche per l'utente (prefs.language), italiano se non l'ha mai mandata."""
        stored = self.data["prefs"].get(user_id or "", {}).get("language")
        return texts.normalize_language(stored) or DEFAULT_LANGUAGE

    def dnd_until(self, user_id: str) -> str | None:
        until = self.data["dnd"].get(user_id)
        if not until:
            return None
        parsed = dt_util.parse_datetime(until)
        if parsed is None or parsed <= dt_util.utcnow():
            self.data["dnd"].pop(user_id, None)
            return None
        return until

    def me_of(self, user_id: str) -> dict[str, Any]:
        prefs = self.prefs_of(user_id)
        return {
            "id": user_id,
            "role": self.role_of(user_id),
            "notify": {
                "shopping_started": prefs["shopping_started"],
                "added_during_shopping": prefs["added_during_shopping"],
            },
            "firefly_always": prefs["firefly_always"],
            "dnd_until": self.dnd_until(user_id),
        }

    @property
    def firefly_enabled(self) -> bool:
        ff = self.options.get(OPT_FIREFLY) or {}
        return bool(ff.get("url") and ff.get("token"))

    # ------------------------------------------------------------------ snapshot

    def _item_visible(self, item: dict[str, Any], role: str) -> bool:
        if role == ROLE_JUNIOR and item.get("visibility") == "adults":
            return False
        status = item.get("status")
        if status == "removed":
            return _is_today(item.get("removed_at"))
        return True

    def _item_for(self, item: dict[str, Any], role: str) -> dict[str, Any]:
        if role == ROLE_JUNIOR:
            item = dict(item)
            item["price_cents"] = None
        return item

    def snapshot(self, user_id: str) -> dict[str, Any]:
        role = self.require_user(user_id)
        items = [self._item_for(i, role) for i in self.data["items"].values() if self._item_visible(i, role)]
        catalog = sorted(self.data["catalog"].values(), key=lambda c: (-c.get("count", 0), c.get("name", "")))
        if role == ROLE_JUNIOR:
            adult_keys = {i["key"] for i in self.data["items"].values() if i.get("visibility") == "adults"}
            catalog = [c for c in catalog if c["key"] not in adult_keys]
        stores = list(self.data["stores"].values())
        insist = [
            i
            for i in self.data["insistences"].values()
            if user_id in (i.get("from_id"), i.get("to_id")) and _is_today(i.get("created_at"))
        ]
        return {
            "rev": self.data["rev"],
            "server_time": _now(),
            "me": self.me_of(user_id),
            "users": self.active_users(),
            "items": items,
            "sessions": list(self.data["sessions"].values()),
            "stores": stores,
            "catalog": catalog[:CATALOG_MAX],
            "insistences": insist,
            "firefly_enabled": self.firefly_enabled,
        }

    # ------------------------------------------------------------------ iscritti

    @callback
    def subscribe(self, sub: Subscriber) -> Callable[[], None]:
        self._subscribers.append(sub)

        @callback
        def _unsub() -> None:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

        return _unsub

    @callback
    def _changed(self, save: bool = True) -> None:
        if save:
            self.data["rev"] += 1
            self._save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)
        if self._pending_state is None:
            self._pending_state = self.hass.loop.call_later(STATE_DEBOUNCE, self._send_states)

    @callback
    def _send_states(self) -> None:
        self._pending_state = None
        for sub in list(self._subscribers):
            try:
                sub.send_state(self.snapshot(sub.user_id))
            except ListaError:
                continue
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Invio stato fallito")

    @callback
    def notify(
        self,
        to_id: str,
        kind: str,
        *,
        args: dict[str, Any] | None = None,
        text_key: str | None = None,
        title_key: str = "app_title",
        from_id: str | None = None,
        item: dict | None = None,
        insistence: dict | None = None,
        session: dict | None = None,
        expense_id: str | None = None,
    ) -> None:
        """Avvisa to_id (notice WebSocket + push), con testo e titolo nella sua lingua.

        Testo = texts[text_key, o kind se manca], titolo = texts[title_key]. Segnaposto: {name} è il nome
        di from_id (o "Qualcuno" nella lingua di to_id), gli altri arrivano da args.
        """
        if not to_id or self.role_of(to_id) == ROLE_NONE:
            return
        role = self.role_of(to_id)
        if item is not None and role == ROLE_JUNIOR and item.get("visibility") == "adults":
            return
        lang = self.language_of(to_id)
        values = {"name": self.name_of(from_id, lang), **(args or {})}
        text = texts.t(lang, text_key or kind, **values)
        title = texts.t(lang, title_key, **values)
        notice = {
            "id": _new_id(),
            "kind": kind,
            "created_at": _now(),
            "from_id": from_id,
            "text": text,
            "item": self._item_for(item, role) if item else None,
            "insistence": insistence,
            "session": session,
            "expense_id": expense_id,
        }
        for sub in list(self._subscribers):
            if sub.user_id == to_id:
                try:
                    sub.send_notice(notice)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Invio notice fallito")
        if self.push is not None:
            data = {"kind": kind}
            if item:
                data["item_id"] = item["id"]
            if insistence:
                data["insistence_id"] = insistence["id"]
            if expense_id:
                data["expense_id"] = expense_id
            if session:
                data["session_id"] = session["id"]
            tokens = [t for t, d in self.data["devices"].items() if d.get("user_id") == to_id]
            if tokens:
                self.hass.async_create_task(
                    self.push.async_send(
                        tokens,
                        title,
                        text,
                        data,
                        channel="insistenze" if kind == "insist" else "lista",
                    )
                )

    def remove_device_tokens(self, tokens: list[str]) -> None:
        for token in tokens:
            self.data["devices"].pop(token, None)
        self._save()

    # ------------------------------------------------------------------ utilità prodotti

    def _item(self, item_id: str) -> dict[str, Any]:
        item = self.data["items"].get(item_id)
        if item is None:
            raise ListaError("not_found", "Prodotto non trovato.")
        return item

    def _touch(self, item: dict[str, Any], user_id: str | None) -> None:
        item["updated_at"] = _now()
        item["updated_by"] = user_id

    def _reset_open(self, item: dict[str, Any]) -> None:
        item.update(
            status="open",
            taken=0,
            shopper_id=None,
            session_id=None,
            removed_by=None,
            removed_at=None,
            removed_reason=None,
        )

    def _bump_catalog(self, name: str) -> None:
        key = logic.normalize_key(name)
        entry = self.data["catalog"].get(key) or {"name": name.strip(), "key": key, "count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_used"] = _now()
        self.data["catalog"][key] = entry
        if len(self.data["catalog"]) > CATALOG_MAX * 2:
            keep = sorted(self.data["catalog"].values(), key=lambda c: -c["count"])[:CATALOG_MAX]
            self.data["catalog"] = {c["key"]: c for c in keep}

    def _shoppers(self) -> list[str]:
        return [s["user_id"] for s in self.data["sessions"].values()]

    # ------------------------------------------------------------------ comandi lista

    def add(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        role = self.require_user(user_id)
        name = (params.get("name") or "").strip()
        if not name:
            raise ListaError("invalid", "Scrivi il nome del prodotto.")
        item_id = params.get("item_id") or _new_id()
        if item_id in self.data["items"]:
            return {"item": self.data["items"][item_id]}
        now = _now()
        item = {
            "id": item_id,
            "name": name,
            "key": logic.normalize_key(name),
            "quantity": max(1, int(params.get("quantity") or 1)),
            "message": params.get("message") or "",
            "message_level": params.get("message_level") or "info",
            "for_whom": params.get("for_whom") or "all",
            "visibility": "all" if role == ROLE_JUNIOR else (params.get("visibility") or "all"),
            "urgent": bool(params.get("urgent", False)),
            "added_by": user_id,
            "added_at": now,
            "updated_at": now,
            "updated_by": user_id,
            "status": "open",
            "taken": 0,
            "price_cents": None,
            "shopper_id": None,
            "session_id": None,
            "not_found_last": False,
            "removed_by": None,
            "removed_at": None,
            "removed_reason": None,
            "insist_count": 0,
        }
        self.data["items"][item_id] = item
        self._bump_catalog(name)
        prefs_cache = {}
        for shopper in set(self._shoppers()):
            if shopper == user_id:
                continue
            prefs_cache[shopper] = self.prefs_of(shopper)
            if prefs_cache[shopper]["added_during_shopping"]:
                self.notify(shopper, "item_added", args={"item": name}, from_id=user_id, item=item)
        self._changed()
        return {"item": item}

    def update(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        role = self.require_user(user_id)
        item = self._item(params["item_id"])
        if role == ROLE_JUNIOR and item.get("visibility") == "adults":
            raise ListaError("not_found", "Prodotto non trovato.")
        for field in EDITABLE:
            if field not in params:
                continue
            value = params[field]
            if field == "name":
                value = (value or "").strip() or item["name"]
                item["key"] = logic.normalize_key(value)
            elif field == "quantity":
                value = max(1, int(value or 1))
                if item.get("taken", 0) > value:
                    item["taken"] = value
            elif field == "visibility" and role == ROLE_JUNIOR:
                continue
            elif field == "urgent":
                value = bool(value)
            item[field] = value
        if item.get("status") == "removed":
            self._reset_open(item)
        self._touch(item, user_id)
        self._changed()
        return {"item": item}

    def remove(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_user(user_id)
        item = self._item(params["item_id"])
        item.update(status="removed", removed_by=user_id, removed_at=_now(), removed_reason="removed", taken=0)
        self._touch(item, user_id)
        if item["added_by"] != user_id:
            self.notify(item["added_by"], "item_removed", args={"item": item["name"]}, from_id=user_id, item=item)
        self._changed()
        return {"item": item}

    def restore(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_user(user_id)
        item = self._item(params["item_id"])
        self._reset_open(item)
        self._touch(item, user_id)
        self._changed()
        return {"item": item}

    # ------------------------------------------------------------------ spesa

    def _create_store(self, name: str, lat: Any, lon: Any) -> dict[str, Any]:
        store = {
            "id": _new_id(),
            "name": name.strip(),
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "radius_m": 100,
            "aisle": {},
        }
        self.data["stores"][store["id"]] = store
        return store

    def start_shopping(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        store = None
        if params.get("store_id"):
            store = self.data["stores"].get(params["store_id"])
            if store is None:
                raise ListaError("not_found", "Supermercato non trovato.")
        elif params.get("store_name"):
            store = self._create_store(params["store_name"], params.get("lat"), params.get("lon"))
        else:
            raise ListaError("invalid", "Indica il supermercato.")
        session = {
            "id": _new_id(),
            "user_id": user_id,
            "store_id": store["id"],
            "store_name": store["name"],
            "prices": bool(params.get("prices", False)),
            "started_at": _now(),
            "order": [],
        }
        self.data["sessions"][session["id"]] = session
        if self.dnd_until(user_id):
            self.data["dnd"][user_id] = (dt_util.utcnow() + timedelta(hours=DND_SESSION_HOURS)).isoformat()
        public = self._public_session(session)
        for user in self.active_users():
            if user["id"] == user_id or not self.prefs_of(user["id"])["shopping_started"]:
                continue
            self.notify(user["id"], "shopping_started", args={"store": store["name"]}, from_id=user_id, session=public)
        self._changed()
        return {"session": public, "store": store}

    @staticmethod
    def _public_session(session: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in session.items() if k != "order"}

    def _session(self, session_id: str | None) -> dict[str, Any]:
        session = self.data["sessions"].get(session_id or "")
        if session is None:
            raise ListaError("no_session", "Nessuna spesa in corso.")
        return session

    def _apply_check(self, user_id: str, session: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        item = self._item(params["item_id"])
        quantity = int(item.get("quantity", 1))
        taken = max(0, min(int(params.get("taken", 0)), quantity))
        close = bool(params.get("close", False))
        if close and taken == 0:
            taken = quantity
        if close or taken >= quantity:
            item.update(status="taken", taken=taken, shopper_id=user_id, session_id=session["id"])
            if item["key"] not in session["order"]:
                session["order"].append(item["key"])
        elif taken == 0:
            item.update(status="open", taken=0, shopper_id=None, session_id=None)
        else:
            item.update(status="open", taken=taken, shopper_id=user_id, session_id=session["id"])
        item.update(removed_by=None, removed_at=None, removed_reason=None)
        if "price_cents" in params:
            price = params.get("price_cents")
            item["price_cents"] = int(price) if price is not None else None
        self._touch(item, user_id)
        return item

    def _apply_not_found(self, user_id: str, session: dict[str, Any], item_id: str) -> dict[str, Any]:
        item = self._item(item_id)
        item.update(status="not_found", taken=0, shopper_id=user_id, session_id=session["id"])
        self._touch(item, user_id)
        if item["added_by"] != user_id:
            self.notify(item["added_by"], "item_not_found", args={"item": item["name"]}, from_id=user_id, item=item)
        return item

    def _apply_reject(self, user_id: str, session: dict[str, Any], item_id: str) -> dict[str, Any]:
        item = self._item(item_id)
        item.update(
            status="removed",
            taken=0,
            shopper_id=user_id,
            session_id=session["id"],
            removed_by=user_id,
            removed_at=_now(),
            removed_reason="rejected",
        )
        self._touch(item, user_id)
        if item["added_by"] != user_id:
            self.notify(item["added_by"], "item_rejected", args={"item": item["name"]}, from_id=user_id, item=item)
        return item

    def _apply_reopen(self, user_id: str, item_id: str) -> dict[str, Any]:
        item = self._item(item_id)
        self._reset_open(item)
        item["price_cents"] = None
        self._touch(item, user_id)
        return item

    def check(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        item = self._apply_check(user_id, self._session(params.get("session_id")), params)
        self._changed()
        return {"item": item}

    def not_found(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        item = self._apply_not_found(user_id, self._session(params.get("session_id")), params["item_id"])
        self._changed()
        return {"item": item}

    def reject(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        item = self._apply_reject(user_id, self._session(params.get("session_id")), params["item_id"])
        self._changed()
        return {"item": item}

    def reopen(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        item = self._apply_reopen(user_id, params["item_id"])
        self._changed()
        return {"item": item}

    def set_price(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        item = self._item(params["item_id"])
        price = params.get("price_cents")
        item["price_cents"] = int(price) if price is not None else None
        self._touch(item, user_id)
        self._changed()
        return {"item": item}

    def do_not_disturb(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        if params.get("enabled"):
            in_session = user_id in self._shoppers()
            hours = DND_SESSION_HOURS if in_session else DND_NO_SESSION_HOURS
            until = (dt_util.utcnow() + timedelta(hours=hours)).isoformat()
            self.data["dnd"][user_id] = until
        else:
            self.data["dnd"].pop(user_id, None)
            until = None
            if user_id in self._shoppers():
                self._deliver_queued(user_id)
        self._changed()
        return {"until": until}

    def _deliver_queued(self, user_id: str) -> None:
        for ins in self.data["insistences"].values():
            if ins["to_id"] == user_id and ins["status"] == "queued":
                ins["status"] = "delivered"
                self._send_insist(ins)

    def _send_insist(self, ins: dict[str, Any]) -> None:
        item = self.data["items"].get(ins["item_id"])
        self.notify(
            ins["to_id"],
            "insist",
            args={"item": ins["item_name"]},
            title_key="insist_title",
            from_id=ins["from_id"],
            item=item,
            insistence=ins,
        )

    # ------------------------------------------------------------------ insistenze

    def insist(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_user(user_id)
        item = self._item(params["item_id"])
        if item["added_by"] != user_id:
            raise ListaError("not_allowed", "Puoi insistere solo per i prodotti che hai chiesto tu.")
        target = item.get("removed_by") if item["status"] == "removed" else item.get("shopper_id")
        if item["status"] not in ("removed", "not_found") or not target or target == user_id:
            raise ListaError("not_allowed", "Qui non c'è niente per cui insistere.")
        for ins in self.data["insistences"].values():
            if ins["item_id"] == item["id"] and ins["status"] in ("queued", "delivered"):
                raise ListaError("invalid", "Hai già insistito: aspetta la risposta.")
        if int(item.get("insist_count", 0)) >= MAX_INSIST:
            raise ListaError("max_insist", "Arrenditi: hai già insistito due volte.")
        item["insist_count"] = int(item.get("insist_count", 0)) + 1
        queued = self.dnd_until(target) is not None
        ins = {
            "id": _new_id(),
            "item_id": item["id"],
            "item_name": item["name"],
            "from_id": user_id,
            "to_id": target,
            "number": item["insist_count"],
            "status": "queued" if queued else "delivered",
            "created_at": _now(),
            "answered_at": None,
            "session_id": item.get("session_id"),
        }
        self.data["insistences"][ins["id"]] = ins
        if not queued:
            self._send_insist(ins)
        self._prune_insistences()
        self._changed()
        return {"insistence": ins}

    def _prune_insistences(self) -> None:
        if len(self.data["insistences"]) < 500:
            return
        keep = sorted(self.data["insistences"].values(), key=lambda i: i["created_at"])[-200:]
        self.data["insistences"] = {i["id"]: i for i in keep}

    def insist_answer(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_user(user_id)
        ins = self.data["insistences"].get(params.get("insistence_id", ""))
        if ins is None:
            raise ListaError("not_found", "Insistenza non trovata.")
        if ins["to_id"] != user_id:
            raise ListaError("not_allowed", "Questa insistenza non è per te.")
        accept = bool(params.get("accept"))
        ins["status"] = "accepted" if accept else "refused"
        ins["answered_at"] = _now()
        item = self.data["items"].get(ins["item_id"])
        if accept and item is not None:
            self._reset_open(item)
            self._touch(item, user_id)
        self.notify(
            ins["from_id"],
            "insist_answer",
            args={"item": ins["item_name"]},
            text_key="insist_accepted" if accept else "insist_refused",
            from_id=user_id,
            item=item,
            insistence=ins,
        )
        self._changed()
        return {"insistence": ins}

    # ------------------------------------------------------------------ fine spesa

    def end_shopping(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        silent = params.get("silent")
        if silent:
            store = None
            if silent.get("store_id"):
                store = self.data["stores"].get(silent["store_id"])
            if store is None and silent.get("store_name") and silent.get("lat") is not None:
                store = self._create_store(silent["store_name"], silent.get("lat"), silent.get("lon"))
            session = {
                "id": _new_id(),
                "user_id": user_id,
                "store_id": store["id"] if store else None,
                "store_name": store["name"] if store else (silent.get("store_name") or "Supermercato"),
                "prices": bool(silent.get("prices", False)),
                "started_at": silent.get("started_at") or _now(),
                "order": [],
            }
        else:
            session = self._session(params.get("session_id"))
        for action in params.get("actions") or []:
            try:
                op = action.get("op")
                if op == "check":
                    self._apply_check(user_id, session, action)
                elif op == "not_found":
                    self._apply_not_found(user_id, session, action["item_id"])
                elif op == "reject":
                    self._apply_reject(user_id, session, action["item_id"])
                elif op == "reopen":
                    self._apply_reopen(user_id, action["item_id"])
            except ListaError as err:
                _LOGGER.debug("Azione ignorata (%s): %s", err.code, action)

        sid = session["id"]
        session_items = [i for i in self.data["items"].values() if i.get("session_id") == sid]
        # Presi in parte e non chiusi: valgono come presi.
        for item in session_items:
            if item["status"] == "open" and int(item.get("taken", 0)) > 0:
                item["status"] = "taken"
        taken_items = [i for i in session_items if i["status"] == "taken"]
        nf_items = [i for i in session_items if i["status"] == "not_found"]
        open_items = [i for i in self.data["items"].values() if i["status"] == "open" and i.get("session_id") != sid]
        counts = logic.end_counts(taken_items + nf_items + open_items)

        lines = [
            {
                "item_id": i["id"],
                "name": i["name"],
                "key": i["key"],
                "quantity": i.get("quantity", 1),
                "taken": i.get("taken", 0) or i.get("quantity", 1),
                "price_cents": i.get("price_cents"),
                "for_whom": i.get("for_whom", "all"),
                "requested_by": i.get("added_by"),
            }
            for i in taken_items
        ]
        receipt_total = params.get("receipt_total_cents")
        payer_id = params.get("payer_id") or user_id
        split = logic.split_costs(lines, receipt_total, payer_id, self.adult_ids(), self.junior_adult)
        for line, who in zip(lines, split["charged_to"]):
            line["charged_to"] = who
        expense = {
            "id": _new_id(),
            "session_id": sid,
            "store_id": session.get("store_id"),
            "store_name": session.get("store_name"),
            "started_at": session.get("started_at"),
            "ended_at": _now(),
            "shopper_id": user_id,
            "payer_id": payer_id,
            "receipt_total_cents": int(receipt_total) if receipt_total is not None else None,
            "marked_total_cents": split["marked_total_cents"],
            "total_cents": split["total_cents"],
            "requested_count": counts["requested_count"],
            "taken_count": counts["taken_count"],
            "not_found": counts["not_found"],
            "lines": lines,
            "shares": split["shares"],
            "receipt": None,
            "firefly_id": None,
        }
        photo_b64 = params.get("receipt_photo_b64")
        if photo_b64:
            self.hass.async_create_task(self.async_save_photo(expense["id"], photo_b64))
            expense["receipt"] = {"has_photo": True, "lines": [], "lines_total_cents": 0, "mismatch_cents": 0}
        self.data["expenses"].insert(0, expense)

        for item in taken_items:
            self.data["items"].pop(item["id"], None)
        for item in nf_items:
            self._reset_open(item)
            item["not_found_last"] = True
        for item in self.data["items"].values():
            item["insist_count"] = 0
            if item.get("session_id") == sid and item["status"] == "removed":
                item["session_id"] = None

        store = self.data["stores"].get(session.get("store_id") or "")
        if store is not None and session.get("order"):
            store["aisle"] = logic.update_aisle(store.get("aisle", {}), session["order"])
        self.data["sessions"].pop(sid, None)
        self.data["dnd"].pop(user_id, None)

        firefly = params.get("firefly")
        if firefly == "always":
            self.data["prefs"].setdefault(user_id, {})["firefly_always"] = True
        want_ff = firefly in ("yes", "always") or (firefly is None and self.prefs_of(user_id)["firefly_always"])
        if want_ff and self.firefly_enabled and self.firefly is not None:
            self.hass.async_create_task(self._async_firefly_create(expense["id"]))

        for adult in self.adult_ids():
            self.notify(
                adult,
                "shopping_ended",
                args={"store": expense["store_name"]},
                from_id=user_id,
                expense_id=expense["id"],
            )
        self._changed()
        return {"expense": expense}

    # ------------------------------------------------------------------ supermercati

    def store_save(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        if params.get("store_id"):
            store = self.data["stores"].get(params["store_id"])
            if store is None:
                raise ListaError("not_found", "Supermercato non trovato.")
            if params.get("name"):
                store["name"] = params["name"].strip()
            if params.get("lat") is not None:
                store["lat"] = float(params["lat"])
            if params.get("lon") is not None:
                store["lon"] = float(params["lon"])
        else:
            if not params.get("name"):
                raise ListaError("invalid", "Scrivi il nome del supermercato.")
            store = self._create_store(params["name"], params.get("lat"), params.get("lon"))
        self._changed()
        return {"store": store}

    def store_delete(self, user_id: str, params: dict[str, Any]) -> None:
        self.require_adult(user_id)
        self.data["stores"].pop(params.get("store_id", ""), None)
        self.data["receipt_maps"].pop(params.get("store_id", ""), None)
        self._changed()

    # ------------------------------------------------------------------ storico e scontrino

    def _expense(self, expense_id: str) -> dict[str, Any]:
        for exp in self.data["expenses"]:
            if exp["id"] == expense_id:
                return exp
        raise ListaError("not_found", "Spesa non trovata.")

    def history(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        limit = int(params.get("limit") or 30)
        result = []
        for exp in self.data["expenses"][:limit]:
            short = dict(exp)
            if short.get("receipt"):
                short["receipt"] = {**short["receipt"], "lines": []}
            result.append(short)
        return {"expenses": result}

    def expense(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        return {"expense": self._expense(params["expense_id"])}

    def receipt_match(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        exp = self._expense(params["expense_id"])
        store_map = self.data["receipt_maps"].get(exp.get("store_id") or "", {})
        return {"lines": logic.match_receipt(params.get("lines") or [], exp.get("lines", []), store_map)}

    def receipt_save(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        exp = self._expense(params["expense_id"])
        lines = params.get("lines") or []
        by_id = {l["item_id"]: l for l in exp.get("lines", [])}
        store_id = exp.get("store_id")
        if store_id:
            store_map = self.data["receipt_maps"].setdefault(store_id, {})
            for line in lines:
                if line.get("kind") == "item" and line.get("item_id") in by_id:
                    key = logic.receipt_text_key(line.get("text", ""))
                    if key:
                        store_map[key] = by_id[line["item_id"]]["key"]
        prices = logic.receipt_prices(lines)
        for item_id, price in prices.items():
            if item_id in by_id:
                by_id[item_id]["price_cents"] = price
        lines_total = sum(int(l.get("price_cents", 0)) for l in lines)
        total = exp.get("receipt_total_cents")
        if total is None:
            total = lines_total
        split = logic.split_costs(exp["lines"], total, exp["payer_id"], self.adult_ids(), self.junior_adult)
        for line, who in zip(exp["lines"], split["charged_to"]):
            line["charged_to"] = who
        exp["marked_total_cents"] = split["marked_total_cents"]
        exp["total_cents"] = split["total_cents"]
        exp["shares"] = split["shares"]
        has_photo = bool((exp.get("receipt") or {}).get("has_photo"))
        if params.get("photo_b64"):
            has_photo = True
            self.hass.async_create_task(self.async_save_photo(exp["id"], params["photo_b64"]))
        exp["receipt"] = {
            "has_photo": has_photo,
            "lines": [
                {
                    "text": l.get("text", ""),
                    "price_cents": int(l.get("price_cents", 0)),
                    "kind": l.get("kind", "item"),
                    "item_id": l.get("item_id"),
                    "match": "confirmed" if l.get("item_id") else l.get("match", "none"),
                }
                for l in lines
            ],
            "lines_total_cents": lines_total,
            "mismatch_cents": int(total) - lines_total,
        }
        if exp.get("firefly_id") and self.firefly is not None and self.firefly_enabled:
            self.hass.async_create_task(self._async_firefly_update(exp["id"]))
        self._changed()
        return {"expense": exp}

    def _photo_path(self, expense_id: str) -> str:
        safe = "".join(c for c in expense_id if c.isalnum())
        return self.hass.config.path(".storage", RECEIPTS_DIR, f"{safe}.jpg")

    async def async_save_photo(self, expense_id: str, photo_b64: str) -> None:
        path = self._photo_path(expense_id)

        def _write() -> None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(photo_b64))

        try:
            await self.hass.async_add_executor_job(_write)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Salvataggio foto scontrino fallito")

    async def async_read_photo(self, expense_id: str) -> bytes | None:
        path = self._photo_path(expense_id)

        def _read() -> bytes | None:
            if not os.path.exists(path):
                return None
            with open(path, "rb") as handle:
                return handle.read()

        return await self.hass.async_add_executor_job(_read)

    async def async_receipt_photo(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        self._expense(params["expense_id"])
        data = await self.async_read_photo(params["expense_id"])
        return {"photo_b64": base64.b64encode(data).decode() if data else None}

    # ------------------------------------------------------------------ saldo

    def balances(self, user_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require_adult(user_id)
        result = logic.compute_balances(self.data["expenses"], self.data["settlements"], self.adult_ids())
        result["settlements"] = self.data["settlements"][-50:]
        return result

    def settle(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_adult(user_id)
        amount = int(params.get("amount_cents") or 0)
        if amount <= 0 or not params.get("from_id") or not params.get("to_id"):
            raise ListaError("invalid", "Importo non valido.")
        self.data["settlements"].append(
            {
                "id": _new_id(),
                "from_id": params["from_id"],
                "to_id": params["to_id"],
                "amount_cents": amount,
                "created_at": _now(),
                "created_by": user_id,
            }
        )
        self._changed()
        return self.balances(user_id)

    # ------------------------------------------------------------------ dispositivi e preferenze

    def register_device(self, user_id: str, params: dict[str, Any]) -> None:
        self.require_user(user_id)
        token = params.get("token")
        if not token:
            raise ListaError("invalid", "Token mancante.")
        self.data["devices"][token] = {
            "user_id": user_id,
            "platform": params.get("platform", "android"),
            "name": params.get("name", ""),
            "updated_at": _now(),
        }
        self._save()

    def unregister_device(self, user_id: str, params: dict[str, Any]) -> None:
        self.data["devices"].pop(params.get("token", ""), None)
        self._save()

    def set_prefs(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Cambia solo i campi presenti. La lingua non è nello stato: se cambia solo lei si salva e basta."""
        self.require_user(user_id)
        current = self.prefs_of(user_id)
        changes: dict[str, Any] = {}
        for key in ("shopping_started", "added_during_shopping", "firefly_always"):
            if params.get(key) is not None and bool(params[key]) != current[key]:
                changes[key] = bool(params[key])
        if params.get("language") is not None:
            language = texts.normalize_language(params["language"])
            if language is None:
                _LOGGER.debug("Lingua ignorata: %r", params["language"])
            elif language != self.data["prefs"].get(user_id, {}).get("language"):
                changes["language"] = language
        if changes:
            self.data["prefs"].setdefault(user_id, {}).update(changes)
            if changes.keys() - {"language"}:
                self._changed()  # cambia "me" nello stato degli iscritti
            else:
                self._save()
        return {"me": self.me_of(user_id)}

    # ------------------------------------------------------------------ Firefly III

    async def _async_firefly_create(self, expense_id: str) -> None:
        try:
            exp = self._expense(expense_id)
            photo = await self.async_read_photo(expense_id) if (exp.get("receipt") or {}).get("has_photo") else None
            ff_id = await self.firefly.async_create(exp, photo)
            if ff_id:
                exp["firefly_id"] = ff_id
                self._changed()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Registrazione in Firefly III fallita")

    async def _async_firefly_update(self, expense_id: str) -> None:
        try:
            exp = self._expense(expense_id)
            photo = await self.async_read_photo(expense_id) if (exp.get("receipt") or {}).get("has_photo") else None
            await self.firefly.async_update(exp, photo)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Aggiornamento Firefly III fallito")

    # ------------------------------------------------------------------ per todo e sensore

    def open_items(self) -> list[dict[str, Any]]:
        return [i for i in self.data["items"].values() if i["status"] in ("open", "not_found")]

    def todo_items(self) -> list[dict[str, Any]]:
        return [i for i in self.data["items"].values() if i["status"] in ("open", "not_found", "taken")]

    def default_user(self) -> str | None:
        adults = self.adult_ids()
        return adults[0] if adults else None

    def expired_dnd_check(self) -> None:
        for user_id in list(self.data["dnd"].keys()):
            self.dnd_until(user_id)

    @staticmethod
    def parse_time(value: str | None) -> datetime | None:
        return dt_util.parse_datetime(value) if value else None
