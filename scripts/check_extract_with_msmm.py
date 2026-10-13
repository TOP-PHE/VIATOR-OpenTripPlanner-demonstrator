"""Check stop extracts with the station module's own rule (manual; not run in CI).

    python scripts/check_extract_with_msmm.py --msmm <MSMM checkout> [--feed-key K FILE ...]

The station module (MSMM, a separate, private repository) refuses an extract
whole at the first fault (`app/master/feed_intake.py::check_extract`). This
script loads that function from a local MSMM checkout, without its database
part, and runs it:

  * with no FILE: on three extracts made here from the invented archives of
    `tests/unit/test_station_extract.py` (GTFS, NeTEx EPIP, NeTEx Nordic);
  * with `--feed-key` and FILEs: on extracts you name (for example one from
    `inbox/<sid>/_extracts/` on a test machine).

It prints one line per extract, `OK <rows>` or `REFUSED <word> <detail>`, and
exits 1 on a refusal. MSMM's details name a line and a column, never a value.
Nothing of MSMM is copied into this repository: the file is read from the
checkout at run time.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import types
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# What MSMM's module imports for its database part, which the check never uses.
_SKIPPED_IMPORTS = ("from sqlalchemy", "from ..models", "from . import station_store")
_CHECK_ENDS_AT = "# ── The rate limit of the PUT"


def load_msmm_check(checkout: Path) -> types.ModuleType:
    source = (checkout / "app" / "master" / "feed_intake.py").read_text(encoding="utf-8")
    head, found, _ = source.partition(_CHECK_ENDS_AT)
    if not found:
        raise SystemExit("MSMM's feed_intake.py has changed shape: update this script")
    kept = "\n".join(line for line in head.splitlines() if not line.startswith(_SKIPPED_IMPORTS))
    module = types.ModuleType("msmm_feed_intake")
    sys.modules[module.__name__] = module  # dataclasses look their module up
    # A file of the local checkout the operator names, run by hand, never in CI.
    exec(compile(kept, "msmm_feed_intake.py", "exec"), module.__dict__)  # noqa: S102  # nosec B102
    return module


def invented_extracts(folder: Path) -> list[tuple[str, Path]]:
    from app.station_extract import build_extract, feed_key_for
    from tests.unit import test_station_extract as fx

    when = datetime.now(UTC)
    made = []
    for fmt, archive in (
        ("gtfs", fx._gtfs(folder)),
        (
            "netex_epip",
            fx._zip(
                folder / "epip.zip",
                {"zz.xml": fx._delivery(fx.EPIP_SITE + fx.EPIP_SERVICE + fx.EPIP_TIMETABLE)},
            ),
        ),
        (
            "netex_nordic",
            fx._zip(folder / "nordic.zip", {"a.xml": fx.NORDIC_SHARED, "b.xml": fx.NORDIC_LINE}),
        ),
    ):
        provider = fx._provider(fmt)
        result = build_extract(
            provider, archive, fx.SHA_A, extracted_at=when, cache_dir=folder / fmt
        )
        made.append((str(feed_key_for(provider)), result.path_gz))
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--msmm", type=Path, required=True, help="a local MSMM checkout")
    parser.add_argument("--feed-key", help="the feed key of the FILEs")
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args()
    msmm = load_msmm_check(args.msmm)
    with tempfile.TemporaryDirectory() as tmp:
        if args.files:
            if not args.feed_key:
                parser.error("--feed-key is needed with FILEs")
            todo = [(args.feed_key, path) for path in args.files]
        else:
            todo = invented_extracts(Path(tmp))
        failed = False
        for feed_key, path in todo:
            try:
                checked = msmm.check_extract(path, feed_key)
            except msmm.ExtractRefused as exc:
                print(f"REFUSED {path.name} {exc.code} {exc.detail}")
                failed = True
            else:
                print(f"OK {path.name} rows={checked.rows}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
