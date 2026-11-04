"""Provider adapters: the only code that knows an engine's journey format (#303).

Each turns one engine's raw answer into `list[CanonicalJourney]`: `motis_adapter`, `otp_adapter`,
`hafas_adapter` (ÖBB, for `/journey` and coverage), `ojp_adapter` (Swiss OJP). They are pure:
no I/O, no database, no station resolution (`canonical.station_resolution` does that).

`trip_dict` is the temporary bridge back to today's dicts, removed with #303 PR 4.
Design: docs/canonical-journey-design.md §5.
"""
