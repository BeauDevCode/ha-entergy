"""Constants for the Entergy integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "entergy_mobile"

CONF_ACCOUNT_ID: Final = "account_id"
CONF_LANGUAGE: Final = "language"
CONF_SCAN_INTERVAL_SECONDS: Final = "scan_interval_seconds"

DEFAULT_LANGUAGE: Final = "en"
DEFAULT_APP_VERSION: Final = "3.59.0"
DEFAULT_SCAN_INTERVAL_SECONDS: Final = 14400
MIN_SCAN_INTERVAL_SECONDS: Final = 3600
MAX_SCAN_INTERVAL_SECONDS: Final = 86400

API_ORIGIN: Final = "https://prod.entergy.mindgrb.io"

STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = "entergy_mobile_usage"

ATTR_ACCOUNT_ID: Final = "account_id"
ATTR_LAST_UPDATE: Final = "last_update"
ATTR_LAST_INTERVAL: Final = "last_interval"
ATTR_IS_ESTIMATED: Final = "is_estimated"
