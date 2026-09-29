"""Closed, value-free repair issue API for Entergy Usage."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_PUBLIC_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)


class RepairKind(StrEnum):
    """Every supported public repair condition."""

    LEDGER_CORRUPT = "ledger_corrupt"
    LEDGER_FUTURE = "ledger_future"
    SCHEMA_DRIFT = "schema_drift"
    CURRENCY_MISMATCH = "currency_mismatch"
    DATA_RETRACTION = "data_retraction"
    LEGACY_ENERGY_SOURCE = "legacy_energy_source"
    LEGACY_ENTITY_ID = "legacy_entity_id"
    BACKFILL_STALLED = "backfill_stalled"


_ERROR_KINDS = {
    RepairKind.LEDGER_CORRUPT,
    RepairKind.LEDGER_FUTURE,
    RepairKind.SCHEMA_DRIFT,
    RepairKind.DATA_RETRACTION,
}


def _issue_id(public_id: str, kind: RepairKind) -> str:
    if _PUBLIC_ID.fullmatch(public_id) is None:
        raise ValueError("invalid public identity")
    if not isinstance(kind, RepairKind):
        raise ValueError("invalid repair kind")
    return f"{kind.value}_{public_id}"


def create_issue(
    hass: HomeAssistant,
    public_id: str,
    kind: RepairKind,
    placeholders: Mapping[str, str] | None = None,
) -> None:
    """Create one persistent value-free repair issue."""
    issue_id = _issue_id(public_id, kind)
    if placeholders:
        raise ValueError("repair placeholders must be value-free")
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=True,
        severity=(ir.IssueSeverity.ERROR if kind in _ERROR_KINDS else ir.IssueSeverity.WARNING),
        translation_key=kind.value,
    )


def delete_issue(hass: HomeAssistant, public_id: str, kind: RepairKind) -> None:
    """Delete one observable repair condition idempotently."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(public_id, kind))
