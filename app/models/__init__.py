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

__all__ = [
    "Base",
    # identity
    "User",
    "UserRole",
    "VerificationToken",
    "PasswordResetToken",
    # sessions
    "Session",
    "SessionCategory",
    "SessionState",
    # ingestion
    "Upload",
    "RebuildJob",
    # graph
    "GraphSnapshot",
    # search
    "JourneySearch",
    "JourneySearchExecution",
    "JourneyTrip",
    # master data
    "MasterStation",
    "MasterStationPendingDrift",
    "RouteAlias",
    "MasterCarrier",
    "MasterCarrierPendingDrift",
    # runtime
    "McTOverride",
    "StationXref",
    # audit
    "AuditEvent",
    # config
    "PlatformConfig",
    # credentials (v0.1.10) — user-owned API keys for authenticated provider URLs
    "UserCredential",
    # NAP catalogues (v0.1.12) — saved NAP endpoints for the import-from-NAP picker
    "NapCatalogue",
    # Network coverage (v0.1.27) — admin matrix runs for systematic
    # all-pairs journey searches across major French rail hubs
    "NetworkCoverageRun",
    "NetworkCoverageResult",
    # Network coverage hubs (v0.1.31) — editable hub catalog (was a
    # NamedTuple constant in app/network_coverage/hubs.py through v0.1.30).
    "NetworkCoverageHub",
]
