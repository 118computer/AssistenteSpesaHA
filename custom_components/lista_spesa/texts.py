"""Frasi per l'utente generate dall'integrazione (notifiche), in ogni lingua.

Una lingua nuova si aggiunge con un dizionario in TEXTS, con le stesse chiavi e gli stessi segnaposto
di "it" ed "en" (lo controlla tests/test_texts.py). Nessun import di Home Assistant: modulo puro.
"""

from __future__ import annotations

from typing import Any

FALLBACK_LANGUAGE = "en"  # lingua usata quando quella dell'utente non c'è

# Segnaposto: {name} = chi ha fatto l'azione, {item} = prodotto, {store} = supermercato.
TEXTS: dict[str, dict[str, str]] = {
    "it": {
        "app_title": "Assistente Spesa",
        "someone": "Qualcuno",
        "item_added": "{name} ha aggiunto {item}",
        "item_removed": "{name} ha tolto {item} dalla lista",
        "shopping_started": "{name} ha iniziato la spesa · {store}",
        "item_not_found": "{name} non ha trovato {item}",
        "item_rejected": "{name} non prende {item}",
        "insist": "{name} insiste per {item}",
        "insist_title": "{name} insiste!",
        "insist_accepted": "{name} prende {item}",
        "insist_refused": "{name}: NO E BASTA per {item}",
        "shopping_ended": "Spesa finita · {store}: abbina lo scontrino quando vuoi",
    },
    "en": {
        "app_title": "Grocery Assistant",
        "someone": "Someone",
        "item_added": "{name} added {item}",
        "item_removed": "{name} removed {item} from the list",
        "shopping_started": "{name} started shopping · {store}",
        "item_not_found": "{name} couldn't find {item}",
        "item_rejected": "{name} won't get {item}",
        "insist": "{name} is nudging you about {item}",
        "insist_title": "{name} is nudging you!",
        "insist_accepted": "{name} will get {item}",
        "insist_refused": "{name} on {item}: NO, AND THAT'S FINAL",
        "shopping_ended": "Shopping finished · {store}: match the receipt whenever you like",
    },
}


class _Args(dict):
    """Segnaposto mancante: resta scritto com'è invece di far fallire il comando."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def normalize_language(value: Any) -> str | None:
    """Codice ISO 639-1 minuscolo dalle prime 2 lettere ("EN-us" → "en"). None se non è un codice."""
    if not isinstance(value, str):
        return None
    code = value.strip().lower()[:2]
    if len(code) == 2 and code.isascii() and code.isalpha():
        return code
    return None


def t(lang: str | None, key: str, **kwargs: Any) -> str:
    """Frase `key` nella lingua `lang`, con i segnaposto riempiti da kwargs.

    Lingua sconosciuta → inglese. Chiave che manca nella lingua → inglese, poi italiano, poi la chiave stessa.
    """
    table = TEXTS.get(normalize_language(lang) or "", TEXTS[FALLBACK_LANGUAGE])
    template = table.get(key) or TEXTS[FALLBACK_LANGUAGE].get(key) or TEXTS["it"].get(key)
    if template is None:
        return key
    return template.format_map(_Args(kwargs))
