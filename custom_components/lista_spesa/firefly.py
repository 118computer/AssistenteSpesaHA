"""Registrazione delle spese in Firefly III (facoltativa). Il token resta in HA."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

CATEGORY = "Alimentari"


def _amount(cents: int) -> str:
    return f"{abs(int(cents)) / 100:.2f}"


class FireflyClient:
    """Crea e aggiorna le uscite in Firefly III."""

    def __init__(self, hass: HomeAssistant, url: str, token: str, account_id: str | None) -> None:
        self.hass = hass
        self.url = url.rstrip("/")
        self.token = token
        self.account_id = account_id

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/json",
        }

    async def async_test(self) -> bool:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(f"{self.url}/api/v1/about", headers=self._headers) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _split(self, expense: dict[str, Any]) -> dict[str, Any]:
        ended = dt_util.parse_datetime(expense.get("ended_at") or "") or dt_util.utcnow()
        split: dict[str, Any] = {
            "type": "withdrawal",
            "date": dt_util.as_local(ended).isoformat(),
            "amount": _amount(expense.get("total_cents", 0)),
            "description": f"Spesa {expense.get('store_name') or ''}".strip(),
            "destination_name": expense.get("store_name") or "Supermercato",
            "category_name": CATEGORY,
            "notes": "Registrata da Assistente Spesa - HA",
        }
        if self.account_id:
            split["source_id"] = str(self.account_id)
        return split

    async def async_create(self, expense: dict[str, Any], photo: bytes | None) -> str | None:
        session = async_get_clientsession(self.hass)
        body = {"error_if_duplicate_hash": False, "apply_rules": True, "transactions": [self._split(expense)]}
        async with session.post(f"{self.url}/api/v1/transactions", json=body, headers=self._headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status not in (200, 201):
                _LOGGER.error("Firefly III: uscita non creata (%s): %s", resp.status, str(data)[:300])
                return None
        tx_id = str(data["data"]["id"])
        journal_id = data["data"]["attributes"]["transactions"][0].get("transaction_journal_id")
        if photo and journal_id:
            await self._attach(journal_id, photo)
        return f"{tx_id}:{journal_id}"

    async def async_update(self, expense: dict[str, Any], photo: bytes | None) -> None:
        ff_id = expense.get("firefly_id")
        if not ff_id:
            return
        tx_id, _, journal_id = ff_id.partition(":")
        split = self._split(expense)
        split["transaction_journal_id"] = journal_id
        session = async_get_clientsession(self.hass)
        async with session.put(
            f"{self.url}/api/v1/transactions/{tx_id}",
            json={"apply_rules": True, "transactions": [split]},
            headers=self._headers,
        ) as resp:
            if resp.status not in (200, 201):
                _LOGGER.warning("Firefly III: aggiornamento fallito (%s)", resp.status)
        if photo and journal_id:
            await self._attach(journal_id, photo)

    async def _attach(self, journal_id: str, photo: bytes) -> None:
        session = async_get_clientsession(self.hass)
        meta = {
            "filename": "scontrino.jpg",
            "attachable_type": "TransactionJournal",
            "attachable_id": str(journal_id),
            "title": "Scontrino",
        }
        async with session.post(f"{self.url}/api/v1/attachments", json=meta, headers=self._headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status not in (200, 201):
                _LOGGER.warning("Firefly III: allegato non creato (%s)", resp.status)
                return
        att_id = data["data"]["id"]
        headers = dict(self._headers)
        headers["Content-Type"] = "application/octet-stream"
        async with session.post(f"{self.url}/api/v1/attachments/{att_id}/upload", data=photo, headers=headers) as resp:
            if resp.status not in (200, 204):
                _LOGGER.warning("Firefly III: caricamento foto fallito (%s)", resp.status)
