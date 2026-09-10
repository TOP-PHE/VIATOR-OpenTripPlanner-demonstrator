"""Single source of truth for routing-engine versions.

Versions are **pinned deliberately**. `:latest` is a trap here, and it bit us:
Docker fetches `:latest` only when the image is absent locally, and the deploy
recipe pulls `web` and `worker` only — never a MOTIS image. So a floating tag
never moves and never gets reviewed. Production served MOTIS **v2.10.2 from
2026-05-30** until 2026-09-10, three minor versions behind, while the registry's
`latest` had long since moved to 2.11.2. A floating tag that nothing pulls gives
neither currency nor reproducibility.

**Two references must agree**, or a session builds its graph with one version and
serves it with another:

* the build containers in ``worker.run_build_motis`` (``motis config`` / ``import``)
* the serve container in ``sessions_orchestrator._MOTIS_SVC_TEMPLATE``

`docs/architecture.md` already warned that pinning one lets the other drift, which
is why both now read from this module rather than spelling the image out twice.

Bump `MOTIS_VERSION` to upgrade — a one-line, reviewable diff, the same discipline
already applied to OTP's ``ARG OTP_VERSION`` and to the SHA-pinned GitHub Actions.
The environment override exists to try a candidate build without a code change;
it is meant to be temporary, and anything long-lived belongs in this file.

Before adopting a new version, re-measure what the MCT adapters depend on: engine
capabilities are properties of a *version*, not of an engine. `mct-tests/` is the
probe. See the MCT design, chapter 10.
"""

import os

# GHCR image tags carry no `v` — release tags are `v2.11.2`, image tags `2.11.2`.
MOTIS_VERSION = os.environ.get("VIATOR_MOTIS_VERSION", "2.11.2")
MOTIS_IMAGE = f"ghcr.io/motis-project/motis:{MOTIS_VERSION}"

# OTP is pinned in `docker/otp/Dockerfile` (``ARG OTP_VERSION``) because VIATOR
# builds its own OTP image rather than consuming an upstream one. Recorded here
# so both engines' pinned versions are discoverable from one place; the
# Dockerfile remains authoritative for the build.
OTP_VERSION = "2.9.0"
