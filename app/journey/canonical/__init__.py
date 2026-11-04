"""CanonicalJourney V1 — VIATOR's provider-neutral journey model (#303).

Design: docs/canonical-journey-design.md. OJP 2.0 Trip semantics, Pydantic v2 frozen models.
Only `app/journey/canonical_adapters/` knows a provider's format; everything here is neutral and
imports no engine client (pinned by `tests/unit/test_provider_neutrality.py`).
"""

from .canonical_journey import SCHEMA_VERSION, CanonicalJourney, JourneySource
from .canonical_leg import CanonicalLeg, LegKind
from .canonical_location import CanonicalLocation, LocationKind
from .canonical_operator import CanonicalOperator
from .canonical_quay import CanonicalQuay
from .canonical_service import CanonicalService, ProductCategory, TransportMode, mode_family
from .canonical_stop_place import CanonicalStopPlace, GeoPosition
from .canonical_timing import CanonicalTiming
from .canonical_transfer import CanonicalTransfer
from .comparison_metadata import ComparisonMetadata
from .journey_fingerprint import JourneyFingerprint, SpineLeg, station_key
from .journey_match_features import JourneyMatchFeatures
from .journey_similarity_result import JourneySimilarityResult, MatchReason, MatchVerdict

__all__ = [
    "SCHEMA_VERSION",
    "CanonicalJourney",
    "CanonicalLeg",
    "CanonicalLocation",
    "CanonicalOperator",
    "CanonicalQuay",
    "CanonicalService",
    "CanonicalStopPlace",
    "CanonicalTiming",
    "CanonicalTransfer",
    "ComparisonMetadata",
    "GeoPosition",
    "JourneyFingerprint",
    "JourneyMatchFeatures",
    "JourneySimilarityResult",
    "JourneySource",
    "LegKind",
    "LocationKind",
    "MatchReason",
    "MatchVerdict",
    "ProductCategory",
    "SpineLeg",
    "TransportMode",
    "mode_family",
    "station_key",
]
