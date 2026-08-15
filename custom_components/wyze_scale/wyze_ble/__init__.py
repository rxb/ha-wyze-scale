"""Standalone BLE protocol library for the Wyze Scale X (WL_SC3)."""

from .client import WyzeScaleClient, WyzeScaleError
from .protocol import (
    CHAR_UUID,
    LOCAL_NAME,
    MANUFACTURER_ID,
    SERVICE_UUID,
    UNIT_KG,
    UNIT_LB,
    Measurement,
    ProtocolError,
    UserRecord,
)

__all__ = [
    "CHAR_UUID",
    "LOCAL_NAME",
    "MANUFACTURER_ID",
    "SERVICE_UUID",
    "UNIT_KG",
    "UNIT_LB",
    "Measurement",
    "ProtocolError",
    "UserRecord",
    "WyzeScaleClient",
    "WyzeScaleError",
]
