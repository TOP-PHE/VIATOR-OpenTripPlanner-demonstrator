"""Editable hub catalog for network-coverage (v0.1.31).

Adds the `network_coverage_hubs` table and seeds it from the hard-coded
26-hub list that lived in `app/network_coverage/hubs.py` through v0.1.30.

The static module remains in place as a legacy fallback (the runner
uses it when the DB table is empty — useful for fresh dev setups and
during this migration's brief window between table-create and seed).

Mapping from the static `Hub.region` values into the new
country/tier/region triple:

  - All 26 existing hubs → country='FR'
  - 25 → tier='main' (TGV/IC headline cities + Paris terminals)
  - 1  → tier='regional' (batz — the small TER halt explicitly added
                          in v0.1.28 as a stress-test pick)
  - region preserved as-is (paris, NE, CE, SE, SW, W, Center)

sort_order is assigned in the curated order from hubs.py — Paris first,
then clockwise around France — so the matrix axis order doesn't change
on first deploy.

Revision ID: 20260505_2200_network_coverage_hubs
Revises: 20260505_1500_network_coverage
Create Date: 2026-05-05 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260505_2200_network_coverage_hubs"
down_revision: str | Sequence[str] | None = "20260505_1500_network_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Seed data mirrors `app/network_coverage/hubs.py` HUBS list as of
# v0.1.30. Defined inline (rather than imported from the app package)
# so the migration runs cleanly even if app/ later drops the module.
# Tuple shape: (id, name, short, region, tier, lat, lon, sort_order).
_SEED_HUBS = [
    # ─── Paris terminals ────────────────────────────────────────────
    ("paris-gdl", "Paris Gare de Lyon", "P-GdL", "paris", "main", 48.8443, 2.3739, 10),
    ("paris-nord", "Paris Gare du Nord", "P-Nord", "paris", "main", 48.8809, 2.3554, 11),
    ("paris-est", "Paris Gare de l'Est", "P-Est", "paris", "main", 48.8767, 2.3593, 12),
    ("paris-mont", "Paris Montparnasse", "P-Mtp", "paris", "main", 48.8410, 2.3219, 13),
    ("paris-aust", "Paris Gare d'Austerlitz", "P-Aust", "paris", "main", 48.8421, 2.3652, 14),
    ("paris-stl", "Paris Saint-Lazare", "P-StL", "paris", "main", 48.8757, 2.3252, 15),
    # ─── North / North-East ─────────────────────────────────────────
    ("lille-flandres", "Lille Flandres", "Lille", "NE", "main", 50.6357, 3.0712, 20),
    ("reims", "Reims", "Reims", "NE", "main", 49.2585, 4.0335, 21),
    ("strasbourg", "Strasbourg", "Strasbourg", "NE", "main", 48.5852, 7.7344, 22),
    ("nancy", "Nancy", "Nancy", "NE", "main", 48.6900, 6.1741, 23),
    # ─── Center-East / Burgundy / Lyon ──────────────────────────────
    ("dijon", "Dijon Ville", "Dijon", "CE", "main", 47.3236, 5.0271, 30),
    ("lyon-pd", "Lyon Part-Dieu", "Lyon-PD", "CE", "main", 45.7607, 4.8593, 31),
    ("clermont", "Clermont-Ferrand", "Clermont", "Center", "main", 45.7708, 3.1024, 32),
    # ─── Mediterranean / South-East ─────────────────────────────────
    ("avignon-tgv", "Avignon TGV", "Avignon", "SE", "main", 43.9215, 4.7860, 40),
    ("aix-tgv", "Aix-en-Provence TGV", "Aix-TGV", "SE", "main", 43.4554, 5.3171, 41),
    ("marseille-stc", "Marseille Saint-Charles", "Marseille", "SE", "main", 43.3026, 5.3801, 42),
    ("nice", "Nice Ville", "Nice", "SE", "main", 43.7045, 7.2614, 43),
    ("montpellier", "Montpellier Saint-Roch", "Montpellier", "SE", "main", 43.6047, 3.8807, 44),
    ("narbonne", "Narbonne", "Narbonne", "SE", "main", 43.1909, 3.0058, 45),
    # ─── South-West / Toulouse / Bordeaux ───────────────────────────
    ("toulouse", "Toulouse Matabiau", "Toulouse", "SW", "main", 43.6112, 1.4537, 50),
    ("bordeaux", "Bordeaux Saint-Jean", "Bordeaux", "SW", "main", 44.8254, -0.5560, 51),
    # ─── Atlantic / West / Brittany ─────────────────────────────────
    ("le-mans", "Le Mans", "Le Mans", "W", "main", 47.9954, 0.1932, 60),
    ("nantes", "Nantes", "Nantes", "W", "main", 47.2173, -1.5424, 61),
    ("rennes", "Rennes", "Rennes", "W", "main", 48.1031, -1.6724, 62),
    ("brest", "Brest", "Brest", "W", "main", 48.3886, -4.4789, 63),
    # batz — the only tier='regional' seed: a small TER halt on the
    # Le Croisic branch, added in v0.1.28 as a "small stations also
    # need to work" stress test.
    ("batz", "Batz-sur-Mer", "Batz", "W", "regional", 47.2774, -2.4844, 64),
]


def upgrade() -> None:
    op.create_table(
        "network_coverage_hubs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("short", sa.String(16), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("region", sa.String(40)),
        sa.Column("tier", sa.String(16), nullable=False, server_default=sa.text("'main'")),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("tier IN ('main','regional')", name="tier_valid"),
    )
    op.create_index(
        "ix_network_coverage_hubs_country_tier",
        "network_coverage_hubs",
        ["country", "tier", "is_active"],
    )
    op.create_index(
        "ix_network_coverage_hubs_active_sort",
        "network_coverage_hubs",
        ["is_active", "sort_order"],
    )

    # Seed the 26 existing hubs. Use bulk_insert so the SQL is plain
    # INSERTs (alembic emits them in a single batch). country='FR' for
    # every seeded row — non-FR hubs are added via the v0.1.31 admin UI
    # post-deploy.
    op.bulk_insert(
        sa.table(
            "network_coverage_hubs",
            sa.column("id", sa.String),
            sa.column("name", sa.String),
            sa.column("short", sa.String),
            sa.column("country", sa.String),
            sa.column("region", sa.String),
            sa.column("tier", sa.String),
            sa.column("lat", sa.Float),
            sa.column("lon", sa.Float),
            sa.column("sort_order", sa.Integer),
        ),
        [
            {
                "id": hub_id,
                "name": name,
                "short": short,
                "country": "FR",
                "region": region,
                "tier": tier,
                "lat": lat,
                "lon": lon,
                "sort_order": sort_order,
            }
            for hub_id, name, short, region, tier, lat, lon, sort_order in _SEED_HUBS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_network_coverage_hubs_active_sort", "network_coverage_hubs")
    op.drop_index("ix_network_coverage_hubs_country_tier", "network_coverage_hubs")
    op.drop_table("network_coverage_hubs")
