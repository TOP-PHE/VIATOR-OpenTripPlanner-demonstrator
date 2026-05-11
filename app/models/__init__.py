"""SQLAlchemy ORM models for VIATOR.

Split by concern for readability. Importing this package side-effect-imports
every submodule so `Base.metadata` carries the full schema — Alembic relies
on this for autogenerate and migration validation.
"""

from __future__ import annotations

from .audit import AuditEvent
from .base import Base
from .config import PlatformConfig
from .credentials import UserCredential
from .graph import GraphSnapshot
from .identity import PasswordResetToken, User, UserRole, VerificationToken
from .ingestion import RebuildJob, Upload
from .master import (
    MasterCarrier,
    MasterCarrierPendingDrift,
    MasterStation,
    MasterStationPendingDrift,
    RouteAlias,
)
from .nap_catalogues import NapCatalogue
from .network_coverage import NetworkCoverageHub, NetworkCoverageResult, NetworkCoverageRun
from .runtime import McTOverride, StationXref
from .search import JourneySearch, JourneySearchExecution, JourneyTrip
from .sessions import Session, SessionCategory, SessionState

# Sorted alphabetically to satisfy RUF022. Domain grouping (identity / sessions /
# ingestion / search / master / runtime / etc.) lives in the imports above —
# the imports stay grouped by concern, only this re-export list is flat.
__all__ = [
    "AuditEvent",
    "Base",
    "GraphSnapshot",
    "JourneySearch",
    "JourneySearchExecution",
    "JourneyTrip",
    "MasterCarrier",
    "MasterCarrierPendingDrift",
    "MasterStation",
    "MasterStationPendingDrift",
    "McTOverride",
    "NapCatalogue",
    "NetworkCoverageHub",
    "NetworkCoverageResult",
    "NetworkCoverageRun",
    "PasswordResetToken",
    "PlatformConfig",
    "RebuildJob",
    "RouteAlias",
    "Session",
    "SessionCategory",
    "SessionState",
    "StationXref",
    "Upload",
    "User",
    "UserCredential",
    "UserRole",
    "VerificationToken",
]
