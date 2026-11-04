"""Configurazione: un'unica voce; nelle opzioni ruoli Adulto/Junior, push FCM, Firefly III."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN, OPT_FCM, OPT_FIREFLY, OPT_JUNIOR_ADULT, OPT_ROLES, ROLE_ADULT, ROLE_JUNIOR, ROLE_NONE
from .firefly import FireflyClient
from .push import parse_service_account

ROLE_OPTIONS = [
    SelectOptionDict(value=ROLE_ADULT, label="Adulto"),
    SelectOptionDict(value=ROLE_JUNIOR, label="Junior"),
    SelectOptionDict(value=ROLE_NONE, label="Nessuno (non entra nell'app)"),
]


class ListaSpesaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Aggiunta dell'integrazione."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Assistente Spesa", data={}, options={OPT_ROLES: {}})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ListaSpesaOptionsFlow()


class ListaSpesaOptionsFlow(OptionsFlow):
    """Pagina "Configura": ruoli, notifiche push, Firefly III."""

    def __init__(self) -> None:
        self._labels: dict[str, str] = {}
        self._new: dict[str, Any] = {}

    def _options(self) -> dict[str, Any]:
        return dict(self.config_entry.options)

    def _firefly_available(self) -> bool:
        return bool(self.hass.config_entries.async_entries("firefly_iii"))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["roles", "push"]
        if self._firefly_available() or (self._options().get(OPT_FIREFLY) or {}).get("url"):
            menu.append("firefly")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def _users(self) -> list[tuple[str, str]]:
        users = await self.hass.auth.async_get_users()
        result: list[tuple[str, str]] = []
        seen: dict[str, int] = {}
        for user in users:
            if user.system_generated or not user.is_active:
                continue
            label = user.name or "Utente"
            seen[label] = seen.get(label, 0) + 1
            if seen[label] > 1:
                label = f"{label} ({seen[label]})"
            result.append((label, user.id))
        return result

    async def async_step_roles(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        users = await self._users()
        self._labels = {label: uid for label, uid in users}
        roles = self._options().get(OPT_ROLES, {}) or {}
        if user_input is not None:
            new_roles = {self._labels[label]: role for label, role in user_input.items() if label in self._labels}
            self._new = {**self._options(), OPT_ROLES: new_roles}
            if any(r == ROLE_JUNIOR for r in new_roles.values()):
                return await self.async_step_juniors()
            return self.async_create_entry(data=self._new)
        schema = {
            vol.Required(label, default=roles.get(uid, ROLE_ADULT)): SelectSelector(
                SelectSelectorConfig(options=ROLE_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            )
            for label, uid in users
        }
        return self.async_show_form(step_id="roles", data_schema=vol.Schema(schema))

    async def async_step_juniors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        roles: dict[str, str] = self._new.get(OPT_ROLES, {})
        id_to_label = {uid: label for label, uid in self._labels.items()}
        adults = [uid for uid in self._labels.values() if roles.get(uid, ROLE_ADULT) == ROLE_ADULT]
        juniors = [uid for uid, role in roles.items() if role == ROLE_JUNIOR]
        if user_input is not None:
            mapping = {}
            for junior in juniors:
                label = id_to_label.get(junior, junior)
                chosen = user_input.get(label)
                if chosen:
                    mapping[junior] = chosen
            self._new[OPT_JUNIOR_ADULT] = mapping
            return self.async_create_entry(data=self._new)
        current = self._options().get(OPT_JUNIOR_ADULT, {}) or {}
        adult_options = [SelectOptionDict(value=uid, label=id_to_label.get(uid, uid)) for uid in adults]
        if not adult_options:
            return self.async_create_entry(data=self._new)
        schema = {}
        for junior in juniors:
            default = current.get(junior) if current.get(junior) in adults else adults[0]
            schema[vol.Required(id_to_label.get(junior, junior), default=default)] = SelectSelector(
                SelectSelectorConfig(options=adult_options, mode=SelectSelectorMode.DROPDOWN)
            )
        return self.async_show_form(step_id="juniors", data_schema=vol.Schema(schema))

    async def async_step_push(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self._options().get(OPT_FCM) or ""
        if user_input is not None:
            raw = (user_input.get("service_account") or "").strip()
            if not raw:
                options = self._options()
                options.pop(OPT_FCM, None)
                return self.async_create_entry(data=options)
            if parse_service_account(raw) is None:
                errors["service_account"] = "invalid_service_account"
            else:
                return self.async_create_entry(data={**self._options(), OPT_FCM: raw})
        schema = vol.Schema(
            {
                vol.Optional("service_account", description={"suggested_value": current}): TextSelector(
                    TextSelectorConfig(multiline=True)
                )
            }
        )
        return self.async_show_form(step_id="push", data_schema=schema, errors=errors)

    async def async_step_firefly(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self._options().get(OPT_FIREFLY) or {}
        if user_input is not None:
            url = (user_input.get("url") or "").strip()
            token = (user_input.get("token") or "").strip()
            if not url or not token:
                options = self._options()
                options.pop(OPT_FIREFLY, None)
                return self.async_create_entry(data=options)
            client = FireflyClient(self.hass, url, token, user_input.get("account_id"))
            if not await client.async_test():
                errors["base"] = "firefly_cannot_connect"
            else:
                return self.async_create_entry(
                    data={
                        **self._options(),
                        OPT_FIREFLY: {"url": url, "token": token, "account_id": (user_input.get("account_id") or "").strip()},
                    }
                )
        schema = vol.Schema(
            {
                vol.Optional("url", description={"suggested_value": current.get("url", "")}): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Optional("token", description={"suggested_value": current.get("token", "")}): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional("account_id", description={"suggested_value": current.get("account_id", "")}): str,
            }
        )
        return self.async_show_form(step_id="firefly", data_schema=schema, errors=errors)
