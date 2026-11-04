"""The JSON Schema of CanonicalJourney V1, generated from the models.

The committed copy is `docs/schemas/canonical-journey-v1.schema.json`; a unit test fails when it
differs from what the models give. Regenerate it with:

    python -m app.journey.canonical.schema > docs/schemas/canonical-journey-v1.schema.json

Derived values (first boarding, transfers, fingerprint…) are properties, not fields: they are
never serialised, so a stored journey always reads back.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .canonical_journey import CanonicalJourney

SCHEMA_ID = "https://viator.trackonpath.com/schemas/canonical-journey-v1.schema.json"


def canonical_journey_schema() -> dict[str, Any]:
    schema = CanonicalJourney.model_json_schema(mode="serialization")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
    }


def schema_text() -> str:
    return (
        json.dumps(canonical_journey_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    # Bytes, so the console's encoding (cp1252 on Windows) and newline translation stay out.
    sys.stdout.buffer.write(schema_text().encode("utf-8"))
