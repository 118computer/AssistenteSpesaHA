"""Costanti di Assistente Spesa - HA."""

DOMAIN = "lista_spesa"
STORAGE_KEY = "lista_spesa.data"
STORAGE_VERSION = 1
RECEIPTS_DIR = "lista_spesa_receipts"

ROLE_ADULT = "adult"
ROLE_JUNIOR = "junior"
ROLE_NONE = "none"

OPT_ROLES = "roles"
OPT_JUNIOR_ADULT = "junior_adult"
OPT_FCM = "fcm_service_account"
OPT_FIREFLY = "firefly"

MAX_INSIST = 2
CATALOG_MAX = 300
DND_NO_SESSION_HOURS = 2
DND_SESSION_HOURS = 12
STATE_DEBOUNCE = 0.1

SIGNAL_UPDATED = "lista_spesa_updated"
