"""P1 MOTIS — SessionCreate Pydantic schema.

Pins that:
  * `engine` defaults to 'otp' when omitted (so legacy clients that
    POST without it keep working).
  * Explicit 'motis' is accepted as-is.
  * The model class includes the new field at all (a refactor that
    accidentally dropped it would still type-check otherwise).
"""

from __future__ import annotations

from app.api.admin.sessions import SessionCreate, SessionResponse


def test_session_create_defaults_engine_to_otp_when_omitted():
    body = SessionCreate(id="nap-fr-rail", name="NAP FR rail", category="NAP")
    assert body.engine == "otp"


def test_session_create_accepts_explicit_motis_engine():
    body = SessionCreate(id="nap-de-rail", name="NAP DE rail", category="NAP", engine="motis")
    assert body.engine == "motis"


def test_session_create_accepts_explicit_otp_engine():
    body = SessionCreate(id="nap-fr-rail", name="NAP FR rail", category="NAP", engine="otp")
    assert body.engine == "otp"


def test_session_create_engine_field_is_declared():
    """If a refactor dropped the `engine` field entirely, Pydantic would
    silently coerce any incoming value to nothing. Lock it down explicitly."""
    assert "engine" in SessionCreate.model_fields


def test_session_response_surfaces_engine_from_orm_session():
    """The list-sessions API must include the engine field so the admin
    UI can render the badge column."""
    from types import SimpleNamespace

    fake_row = SimpleNamespace(
        id="x",
        name="X",
        category="NAP",
        state="created",
        engine="motis",
        config={},
        include_in_fanout=False,
        created_at=None,
        archived_at=None,
    )
    resp = SessionResponse.from_orm_session(fake_row)
    assert resp.engine == "motis"


def test_session_response_defaults_engine_to_otp_for_legacy_rows():
    """Defensive: a SessionRow without the engine attribute (e.g. unit
    test fixture predating P1) maps to 'otp' so the response doesn't
    fail to validate."""
    from types import SimpleNamespace

    legacy_row = SimpleNamespace(
        id="x",
        name="X",
        category="NAP",
        state="created",
        engine=None,  # NULL in some old fixtures
        config={},
        include_in_fanout=False,
        created_at=None,
        archived_at=None,
    )
    resp = SessionResponse.from_orm_session(legacy_row)
    assert resp.engine == "otp"
