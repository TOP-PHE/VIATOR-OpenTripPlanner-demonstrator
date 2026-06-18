# MOTIS Phase-0 spike

Exploratory wrapper for the **MOTIS** journey planner
([motis-project/motis](https://github.com/motis-project/motis)) — runs alongside
existing OTP sessions without disturbing them, so the same feed can be queried
head-to-head and we can measure whether MOTIS is worth integrating as a
selectable engine. **Status: spike, no production code path.**

What this dir contains:

- `docker-compose.yml` — one MOTIS container on port **8081** (no clash with OTP
  on 8080). Pinned to `ghcr.io/motis-project/motis:latest`; bump in-file when
  you want a specific tag.
- `config.example.yml` — annotated MOTIS config. `motis config` regenerates a
  fresh one for you the first time; keep this as a reference for what knobs
  exist.
- `compare.py` — small CLI that runs the **same query** against an OTP container
  *and* the MOTIS spike container and prints them side by side (per-leg,
  duration, transfers, latency). Uses `app/journey/otp_client.fetch_plan` and
  `app/journey/motis_client.fetch_plan`, so the translator on the MOTIS side is
  exercised in anger.
- `data/` (gitignored) — where you drop the `osm.pbf` and one or more
  `gtfs*.zip` feeds. The container reads from there.

There is also a draft module **`app/journey/motis_client.py`** that mirrors
`otp_client.fetch_plan`'s signature + return shape, so once the spike validates
the response translation, the dispatcher in Phase 1 is a one-liner change.

## Quick start

1. **Drop the inputs in `motis-spike/data/`**

   ```bash
   mkdir -p motis-spike/data
   cp /path/to/europe-latest.osm.pbf motis-spike/data/osm.pbf
   cp /path/to/sncf-xb.zip motis-spike/data/sncf.gtfs.zip
   # repeat for any extra GTFS feeds you want loaded together
   ```

   For a first-time measurement keep it small (one country's rail-only GTFS +
   the matching country OSM extract). MOTIS imports the planet in <2 min on
   modest hardware, so this is mostly to get fast iteration.

2. **Generate the config (one-off)**

   ```bash
   cd motis-spike
   docker compose run --rm motis config /data/osm.pbf /data/sncf.gtfs.zip
   ```

   Writes `data/config.yml`. Edit if you want — the [example](./config.example.yml)
   shows what's available.

3. **Import (build the index — one-off, fast)**

   ```bash
   docker compose run --rm motis import
   ```

4. **Run the server**

   ```bash
   docker compose up -d
   curl -sS 'http://localhost:8081/' | head            # smoke
   ```

5. **Query head-to-head against your OTP** (assumes one of your serving OTP
   containers is reachable — see `compare.py --help` for how to point at one):

   ```bash
   python motis-spike/compare.py \
       --otp-url http://otp-nap-fr-rail:8080 \
       --motis-url http://localhost:8081 \
       --from 48.844,2.374 \
       --to   43.295,5.376 \
       --when 2026-06-01T08:00:00Z
   ```

   Prints both engines' top itinerary (departure → arrival, duration,
   transfers, modes, the leg spine) plus query latency.

## What we're trying to learn

| Question | Where the answer comes from |
|---|---|
| Does MOTIS load *our* GTFS without surprises? | Step 3 — error logs from `import` |
| Memory + import time on real data | `docker stats` during import; container RAM at idle after `server` |
| Query latency vs OTP | `compare.py` "took N ms" line |
| Result quality — same trains? close? wildly different? | `compare.py` per-leg spine, plus eyeballing the JSON |
| Translator gap — what fields does MOTIS emit that the canonical trip dict doesn't carry, and vice-versa? | TODOs in `app/journey/motis_client.py::_itineraries_to_trips` |

## What this spike deliberately does NOT do

- No engine-selection schema on `session.config` yet (Phase 1).
- No per-session orchestrator support (Phase 1).
- No production wiring of MOTIS into fanout / the federated planner (Phase 1).
- No real-time (GTFS-RT / SIRI) — MOTIS supports it but the OTP path doesn't
  consume it either; leave for Phase 2.

Refer to `docs/cross-border-routing-as-built.md` for the overall as-built
record; this spike will get its own section once we have measurements.
