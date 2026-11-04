"""Notifiche push via Firebase Cloud Messaging HTTP v1, direttamente dall'integrazione (fase 1)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import jwt

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


def parse_service_account(raw: str | dict | None) -> dict[str, Any] | None:
    """Valida il JSON del service account Firebase. None se assente o non valido."""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return None
    if not all(data.get(k) for k in ("project_id", "client_email", "private_key")):
        return None
    return data


class PushSender:
    """Invia messaggi FCM ai token dei telefoni."""

    def __init__(
        self,
        hass: HomeAssistant,
        service_account: dict[str, Any],
        on_invalid_tokens: Callable[[list[str]], None],
    ) -> None:
        self.hass = hass
        self.sa = service_account
        self._on_invalid = on_invalid_tokens
        self._token: str | None = None
        self._token_exp = 0.0

    def _sign(self) -> str:
        now = int(time.time())
        payload = {
            "iss": self.sa["client_email"],
            "scope": SCOPE,
            "aud": self.sa.get("token_uri") or DEFAULT_TOKEN_URI,
            "iat": now,
            "exp": now + 3600,
        }
        return jwt.encode(payload, self.sa["private_key"], algorithm="RS256")

    async def _access_token(self) -> str | None:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        assertion = await self.hass.async_add_executor_job(self._sign)
        session = async_get_clientsession(self.hass)
        async with session.post(
            self.sa.get("token_uri") or DEFAULT_TOKEN_URI,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in body:
                _LOGGER.error("FCM: token OAuth non ottenuto (%s): %s", resp.status, body)
                return None
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 3600))
            return self._token

    async def async_send(
        self, tokens: list[str], title: str, body: str, data: dict[str, Any], channel: str = "lista"
    ) -> None:
        try:
            access = await self._access_token()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("FCM: firma del token fallita (controlla la chiave del service account)")
            return
        if not access:
            return
        url = f"https://fcm.googleapis.com/v1/projects/{self.sa['project_id']}/messages:send"
        session = async_get_clientsession(self.hass)
        invalid: list[str] = []
        str_data = {k: str(v) for k, v in data.items() if v is not None}
        for token in tokens:
            message = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": str_data,
                    "android": {
                        "priority": "HIGH",
                        "notification": {"channel_id": channel, "sound": "default"},
                    },
                    "apns": {
                        "headers": {"apns-priority": "10"},
                        "payload": {"aps": {"sound": "default", "interruption-level": "time-sensitive" if channel == "insistenze" else "active"}},
                    },
                }
            }
            try:
                async with session.post(url, json=message, headers={"Authorization": f"Bearer {access}"}) as resp:
                    if resp.status == 200:
                        continue
                    text = await resp.text()
                    if resp.status == 404 or "UNREGISTERED" in text or (
                        resp.status == 400 and "registration token" in text.lower()
                    ):
                        invalid.append(token)
                    else:
                        _LOGGER.warning("FCM: invio fallito (%s): %s", resp.status, text[:300])
            except Exception:  # noqa: BLE001
                _LOGGER.exception("FCM: errore di rete")
        if invalid:
            self._on_invalid(invalid)
