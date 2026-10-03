"""Station panel: sources, registers, the VIATOR station reference and its builds.

See docs/station-panel-design.md section 3. Three families of tables:

  * sources    `station_source`, `station_source_version`, `station_build`,
               and the code-series vocabulary `station_code_series`;
  * registers  `crd_location`, `crd_subsidiary`, `era_operational_point`,
               one set of rows per acquired source version;
  * reference  `station_ref` and its children. A row is *current state*: its
               identity `(plc, era_uopid)` is stable across builds, and what a
               build changed is written to `station_ref_history`.

Key columns are `text` with `COLLATE "C"`: a PLC is not always two letters and
five digits, `char(n)` would pad it, and a byte-wise order is stable across
locales. Vocabularies that grow with the offline chain (tier, match method,
flag token, source format) are plain text, validated in the API layer or not
at all; only `station_code_series` is a lookup table.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

# Byte-wise collation for natural keys (PLC, operational-point id, codes).
_KEY = Text(collation="C")

_FALSE = text("FALSE")
_TRUE = text("TRUE")
_STATION_FK = "station_ref.id"
_VERSION_FK = "station_source_version.id"
_BUILD_FK = "station_build.id"
_USER_FK = "users.id"
_SET_NULL = "SET NULL"


# ───────────────────────────── sources ─────────────────────────────


class StationCodeSeries(Base):
    """The vocabulary of code series. Data, not a CHECK: extended by INSERT."""

    __tablename__ = "station_code_series"

    key: Mapped[str] = mapped_column(_KEY, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    # Coarser grouping (`uic_intl`, `eva`, `dhid`), filled by a later normalisation.
    family: Mapped[str | None] = mapped_column(Text)
    is_joinable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_TRUE)


class StationSource(TimestampMixin, Base):
    """One row per (portal, dataset) a station input is acquired from."""

    __tablename__ = "station_source"
    __table_args__ = (UniqueConstraint("key", name="uq_station_source_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    key: Mapped[str] = mapped_column(_KEY, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    acquisition: Mapped[str] = mapped_column(Text, nullable=False)
    resolver_type: Mapped[str | None] = mapped_column(Text)
    # Same shape as an entry of app/data/eu19_nap_sources.json.
    resolver_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_credentials.id", ondelete=_SET_NULL)
    )
    country_iso: Mapped[str | None] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str | None] = mapped_column(Text)
    licence_url: Mapped[str | None] = mapped_column(Text)
    access_expires_on: Mapped[date | None] = mapped_column(Date)
    refresh_cadence: Mapped[str | None] = mapped_column(Text)
    triggers_rebuild: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_FALSE)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_TRUE)
    # An offline provider column that aggregates several (portal, dataset)
    # pairs. Its codes import now; splitting it is a later, trackable job.
    source_key_unresolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=_FALSE
    )


class StationSourceVersion(Base):
    """One acquired file of a source, identified by its sha256."""

    __tablename__ = "station_source_version"
    __table_args__ = (
        UniqueConstraint("source_id", "sha256", name="uq_station_source_version_source_id_sha256"),
        Index(
            "ix_station_source_version_source_id_acquired_at",
            "source_id",
            text("acquired_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("station_source.id", ondelete="RESTRICT"), nullable=False
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    as_of: Mapped[date | None] = mapped_column(Date)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'uploaded'"))
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    stored_path: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(_USER_FK, ondelete=_SET_NULL)
    )


class StationBuild(Base):
    """One run of the reference build: its inputs, counts and diff."""

    __tablename__ = "station_build"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    builder_version: Mapped[str | None] = mapped_column(Text)
    # Source version ids and sha256 of every input.
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    diff_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    log_path: Mapped[str | None] = mapped_column(Text)


# ───────────────────────────── registers ─────────────────────────────


class CrdLocation(Base):
    """A CRD primary location, as one source version carries it."""

    __tablename__ = "crd_location"
    __table_args__ = (
        # CRD's own key is (country, code, validity).
        UniqueConstraint(
            "source_version_id",
            "country",
            "location_code",
            "start_validity",
            name="uq_crd_location_version_key",
        ),
        Index("ix_crd_location_source_version_id_plc", "source_version_id", "plc"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(_VERSION_FK, ondelete="CASCADE", name="fk_crd_location_source_version_id"),
        nullable=False,
    )
    country: Mapped[str | None] = mapped_column(_KEY)
    location_code: Mapped[str | None] = mapped_column(_KEY)
    plc: Mapped[str] = mapped_column(_KEY, nullable=False)
    # Kept as published: the offline extractor's date format is not ours to guess.
    start_validity: Mapped[str | None] = mapped_column(Text)
    end_validity: Mapped[str | None] = mapped_column(Text)
    active_flag: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    free_text: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    passenger_flag: Mapped[str | None] = mapped_column(Text)
    freight_flag: Mapped[str | None] = mapped_column(Text)
    responsible_im: Mapped[str | None] = mapped_column(Text)
    nuts: Mapped[str | None] = mapped_column(Text)


class CrdSubsidiary(Base):
    """A subsidiary code of a CRD location (RL100, SNCF, DIUM, ...)."""

    __tablename__ = "crd_subsidiary"
    __table_args__ = (Index("ix_crd_subsidiary_source_version_id_plc", "source_version_id", "plc"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(_VERSION_FK, ondelete="CASCADE", name="fk_crd_subsidiary_source_version_id"),
        nullable=False,
    )
    plc: Mapped[str] = mapped_column(_KEY, nullable=False)
    subsidiary_type: Mapped[str] = mapped_column(Text, nullable=False)
    allocation_company: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str] = mapped_column(_KEY, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)


class EraOperationalPoint(Base):
    """An ERA operational point, as one source version carries it."""

    __tablename__ = "era_operational_point"
    __table_args__ = (
        UniqueConstraint(
            "source_version_id",
            "plc",
            "uopid",
            name="uq_era_operational_point_source_version_id_plc_uopid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            _VERSION_FK, ondelete="CASCADE", name="fk_era_operational_point_source_version_id"
        ),
        nullable=False,
    )
    plc: Mapped[str] = mapped_column(_KEY, nullable=False)
    uopid: Mapped[str] = mapped_column(_KEY, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    op_type: Mapped[str | None] = mapped_column(Text)
    iso2: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    rl100: Mapped[str | None] = mapped_column(Text)


# ───────────────────────────── reference ─────────────────────────────


class StationComplex(TimestampMixin, Base):
    """A grouping of reference rows that passengers treat as one station."""

    __tablename__ = "station_complex"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    rule: Mapped[str | None] = mapped_column(Text)
    # The border-control case: one complex, two halves that must stay apart.
    requires_physical_separation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=_FALSE
    )
    separation_reason: Mapped[str | None] = mapped_column(Text)


class StationRef(Base):
    """VIATOR's station reference: one row per (PLC, operational point)."""

    __tablename__ = "station_ref"
    __table_args__ = (
        # era_uopid is NOT NULL because a unique index treats NULLs as
        # distinct: ('ZZ00001', NULL) would be insertable without limit.
        UniqueConstraint("plc", "era_uopid", name="uq_station_ref_plc_era_uopid"),
        CheckConstraint("length(plc) = 7", name="plc_length"),
        CheckConstraint(
            "complex_role IS NULL OR complex_role IN ('principal','member')",
            name="complex_role_valid",
        ),
        Index("ix_station_ref_iso2_is_passenger", "iso2", "is_passenger"),
        Index("ix_station_ref_uic_merits", "uic_merits"),
        Index("ix_station_ref_previous_plc", "previous_plc"),
        Index("ix_station_ref_complex_id", "complex_id"),
        Index("ix_station_ref_iso2_all", "iso2_all", postgresql_using="gin"),
        Index(
            "ix_station_ref_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_station_ref_alt_name_trgm",
            "alt_name_text",
            postgresql_using="gin",
            postgresql_ops={"alt_name_text": "gin_trgm_ops"},
        ),
        # What step 7 puts behind the journey typeahead: routable rows only.
        Index(
            "ix_station_ref_typeahead",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
            postgresql_where=text("is_passenger AND uic_merits IS NOT NULL"),
        ),
        Index("ix_station_ref_current", "plc", postgresql_where=text("is_current")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Identity
    plc: Mapped[str] = mapped_column(_KEY, nullable=False)
    era_uopid: Mapped[str] = mapped_column(_KEY, nullable=False)
    previous_plc: Mapped[str | None] = mapped_column(_KEY)

    # Names
    name: Mapped[str | None] = mapped_column(Text)
    name_src: Mapped[str | None] = mapped_column(Text)
    alt_name: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # alt_name joined for the trigram index: pg_trgm cannot index a text[].
    alt_name_text: Mapped[str | None] = mapped_column(Text)

    # Position
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    pos_src: Mapped[str | None] = mapped_column(Text)
    link_pos_src: Mapped[str | None] = mapped_column(Text)
    position_flag: Mapped[str | None] = mapped_column(Text)

    # Classification
    iso2: Mapped[str | None] = mapped_column(Text)
    iso2_all: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    op_type_all: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    op_type_src: Mapped[str | None] = mapped_column(Text)
    is_passenger: Mapped[bool | None] = mapped_column(Boolean)
    is_passenger_src: Mapped[str | None] = mapped_column(Text)
    plc_kind: Mapped[str | None] = mapped_column(Text)

    # Multiplicity: how many operational points share this PLC, and how far apart.
    n_op_with_plc: Mapped[int | None] = mapped_column(Integer)
    plc_op_max_sep_m: Mapped[int | None] = mapped_column(Integer)

    # Spine
    spine_source: Mapped[str | None] = mapped_column(Text)
    crd_start: Mapped[date | None] = mapped_column(Date)
    crd_end: Mapped[date | None] = mapped_column(Date)
    # Set by the build (crd_end is null or not yet past). Not a generated
    # column: CURRENT_DATE is not immutable, so Postgres refuses it there.
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_TRUE)
    crd_source_tag: Mapped[str | None] = mapped_column(Text)

    # MERITS: mirrors the chosen row of station_ref_merits, for fast filtering.
    uic_merits: Mapped[str | None] = mapped_column(_KEY)
    uic_merits_origin: Mapped[str | None] = mapped_column(Text)
    uic_merits_rule: Mapped[str | None] = mapped_column(Text)
    uic_merits_confidence: Mapped[str | None] = mapped_column(Text)

    # Other codes
    rl100: Mapped[str | None] = mapped_column(Text)
    nat_code: Mapped[str | None] = mapped_column(Text)
    nat_code_series: Mapped[str | None] = mapped_column(Text)
    nat_code_src: Mapped[str | None] = mapped_column(Text)
    ifopt_dhid: Mapped[str | None] = mapped_column(Text)
    ifopt_dhid_src: Mapped[str | None] = mapped_column(Text)
    eva: Mapped[str | None] = mapped_column(Text)
    eva_src: Mapped[str | None] = mapped_column(Text)
    eva_all: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    # Grouping
    complex_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("station_complex.id", ondelete=_SET_NULL)
    )
    complex_role: Mapped[str | None] = mapped_column(Text)

    # Quality
    warning_level: Mapped[str | None] = mapped_column(Text)
    best_tier: Mapped[str | None] = mapped_column(Text)
    n_nap_feeds: Mapped[int | None] = mapped_column(Integer)

    # Lineage
    first_seen_build_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey(_BUILD_FK, ondelete="RESTRICT")
    )
    last_built_build_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey(_BUILD_FK, ondelete="RESTRICT")
    )
    last_changed_build_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey(_BUILD_FK, ondelete="RESTRICT")
    )


class StationRefCode(Base):
    """One code of a station, as one source publishes it. Stored long, shown wide."""

    __tablename__ = "station_ref_code"
    __table_args__ = (
        # `series` is NULL where the source does not state one; NULLS NOT
        # DISTINCT keeps the constraint meaningful for those rows.
        UniqueConstraint(
            "station_id",
            "source_key",
            "series",
            "code",
            name="uq_station_ref_code_station_id_source_key_series_code",
            postgresql_nulls_not_distinct=True,
        ),
        # A lookup index, NOT a unique key: one (series, code) pair can
        # legitimately belong to more than one station.
        Index("ix_station_ref_code_series_code", "series", "code"),
        Index("ix_station_ref_code_code", "code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(_KEY, nullable=False)
    series: Mapped[str | None] = mapped_column(_KEY, ForeignKey("station_code_series.key"))
    code: Mapped[str] = mapped_column(_KEY, nullable=False)
    # What the provider published, when `code` is a normalised form of it.
    code_raw: Mapped[str | None] = mapped_column(Text)
    normalisation_rule: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_FALSE)
    # A code that must never be used as a join key.
    evidence_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_FALSE)
    confidence: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(Text)


class StationRefMerits(Base):
    """Every MERITS candidate of a station, not only the chosen one."""

    __tablename__ = "station_ref_merits"
    __table_args__ = (
        UniqueConstraint("station_id", "code", name="uq_station_ref_merits_station_id_code"),
        Index(
            "uq_station_ref_merits_chosen",
            "station_id",
            unique=True,
            postgresql_where=text("is_chosen"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(_KEY, nullable=False)
    origin: Mapped[str | None] = mapped_column(Text)
    rule: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    check_digit: Mapped[str | None] = mapped_column(Text)
    is_chosen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_FALSE)


class StationRefAlias(Base):
    """An old PLC that now resolves to this station."""

    __tablename__ = "station_ref_alias"
    __table_args__ = (
        UniqueConstraint("alias_plc", "build_id", name="uq_station_ref_alias_alias_plc_build_id"),
        Index("ix_station_ref_alias_station_id", "station_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete="CASCADE"), nullable=False
    )
    alias_plc: Mapped[str] = mapped_column(_KEY, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    build_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_BUILD_FK, ondelete="CASCADE"), nullable=False
    )


class StationRefFlag(Base):
    """One flag of a station; many name another station, hence a table."""

    __tablename__ = "station_ref_flag"
    __table_args__ = (
        # payload is NOT NULL ('' when the token has none) so that the unique
        # constraint actually constrains payload-less tokens.
        UniqueConstraint(
            "station_id", "token", "payload", name="uq_station_ref_flag_station_id_token_payload"
        ),
        Index("ix_station_ref_flag_token", "token"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    level: Mapped[str | None] = mapped_column(Text)
    warning_code: Mapped[str | None] = mapped_column(Text)
    related_station_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete=_SET_NULL)
    )


class StationRefOverride(Base):
    """A hand correction of one field of one station. Survives rebuilds."""

    __tablename__ = "station_ref_override"
    __table_args__ = (
        Index(
            "uq_station_ref_override_active",
            "station_id",
            "field_name",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # RESTRICT: a station carrying hand corrections is not deleted by accident.
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete="RESTRICT"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    set_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(_USER_FK, ondelete=_SET_NULL)
    )
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # What the build had computed when the correction was made, and what it
    # computes now. The second is what a release restores.
    computed_value_at_set: Mapped[str | None] = mapped_column(Text)
    computed_value_latest: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StationRefLink(Base):
    """One rung of the adjudication ladder: a NAP stop against a reference row.

    A null `station_id` is a stop no reference row could be matched to;
    `nearest_plc` and its distance are the hint the operator works from.
    """

    __tablename__ = "station_ref_link"
    __table_args__ = (
        Index("ix_station_ref_link_station_id", "station_id"),
        Index(
            "ix_station_ref_link_unmatched",
            "label",
            "iso2",
            postgresql_where=text("station_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete="CASCADE")
    )
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(_VERSION_FK, ondelete="CASCADE", name="fk_station_ref_link_source_version_id"),
        nullable=False,
    )
    offline_station_id: Mapped[str | None] = mapped_column(Text)
    feed_key: Mapped[str | None] = mapped_column(Text)
    stop_key: Mapped[str | None] = mapped_column(Text)
    stop_name: Mapped[str | None] = mapped_column(Text)
    iso2: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    label: Mapped[str | None] = mapped_column(Text)
    code_value: Mapped[str | None] = mapped_column(Text)
    code_series: Mapped[str | None] = mapped_column(Text)
    # Free text offline (thousands of distinct values): never a vocabulary.
    match_method: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(Text)
    asserted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=_FALSE)
    distance_m: Mapped[float | None] = mapped_column(Float)
    name_sim: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    nearest_plc: Mapped[str | None] = mapped_column(_KEY)
    nearest_distance_m: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)


class StationRefHistory(Base):
    """What one build changed on one station, field by field.

    `field_name` is a column of `station_ref`, or `codes`, `merits` or `flags`
    when the station's rows in that child table are no longer the ones it had.
    """

    __tablename__ = "station_ref_history"
    __table_args__ = (
        Index("ix_station_ref_history_station_id_build_id", "station_id", "build_id"),
        Index("ix_station_ref_history_build_id", "build_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_STATION_FK, ondelete="CASCADE"), nullable=False
    )
    build_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(_BUILD_FK, ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
