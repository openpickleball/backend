"""Rebuild every rating from the whole counted ledger (#48, ADR 0058).

Run it after an engine upgrade, a change of ``OPEN_ENGINE_PARAMETER_SET``,
or a bump of ``OPEN_RATING_SPEC_VERSION`` (``docs/engine.md``). It is a
full replay through ``apps.ratings.services.recompute_ratings()``, in one
transaction like the regular replay, and safe to run again.

``--dry-run`` writes nothing and prints what would change. Only a dry run
takes ``--format``, an ``--as-of`` before the latest counted match or
after the default replay date, or a ``--spec-version`` other than
``OPEN_RATING_SPEC_VERSION``, so a replay can be compared without
touching the stored ratings. The output names no player: it holds
counts only.

Each run checks in to the ``recompute-ratings`` cron monitor (#65), so a
run that fails or passes the monitor's maximum runtime shows as a failed
monitor. A refused run (bad options) checks in as ok: it wrote nothing. With Sentry off the check-ins go nowhere.
"""

import time
from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from sentry_sdk.crons import MonitorStatus, capture_checkin

from apps.matches.models import RatingFormat
from apps.ratings.services import (
    RecomputeChangeKind,
    RecomputeRefused,
    RecomputeResult,
    recompute_ratings,
)
from config.sentry import RECOMPUTE_MONITOR_CONFIG, RECOMPUTE_MONITOR_SLUG


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError("--as-of must be a date such as 2026-03-31.") from None


class Command(BaseCommand):
    help = (
        "Rebuild every rating with a full replay of the counted ledger. "
        "--dry-run prints what would change and writes nothing."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--as-of",
            help=(
                "Replay as of this date (YYYY-MM-DD). Default: today in UTC, or "
                "the latest counted match date when later. A date before the "
                "latest counted match, or after the default, needs --dry-run."
            ),
        )
        parser.add_argument(
            "--format",
            dest="rating_format",
            choices=RatingFormat.values,
            help="Show only this rating format. Needs --dry-run.",
        )
        parser.add_argument(
            "--spec-version",
            help=(
                "The rating spec version to store. Default: "
                "OPEN_RATING_SPEC_VERSION. Another value needs --dry-run."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Replay and compare with the stored ratings, but write nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        as_of = None if options["as_of"] is None else _date(options["as_of"])
        # Checked in by hand, not with sentry_sdk's monitor(): a refused
        # run (bad options) wrote nothing and is not a failed replay.
        check_in_id = capture_checkin(
            monitor_slug=RECOMPUTE_MONITOR_SLUG,
            monitor_config=RECOMPUTE_MONITOR_CONFIG,
            status=MonitorStatus.IN_PROGRESS,
        )
        started = time.monotonic()
        status = MonitorStatus.ERROR
        try:
            result = recompute_ratings(
                as_of=as_of,
                rating_format=options["rating_format"],
                spec_version=options["spec_version"],
                dry_run=options["dry_run"],
            )
            status = MonitorStatus.OK
        except RecomputeRefused as exc:
            status = MonitorStatus.OK
            raise CommandError(str(exc)) from None
        except Exception as exc:
            # The service logged the class; never print the error's text,
            # which can quote values.
            raise CommandError(
                f"The recompute failed ({type(exc).__name__}). Nothing was written."
            ) from None
        finally:
            capture_checkin(
                monitor_slug=RECOMPUTE_MONITOR_SLUG,
                monitor_config=RECOMPUTE_MONITOR_CONFIG,
                check_in_id=check_in_id,
                status=status,
                duration=time.monotonic() - started,
            )
        self._report(result, int(options["verbosity"]))

    def _report(self, result: RecomputeResult, verbosity: int) -> None:
        heading = "Dry run, nothing written" if result.dry_run else "Recomputed"
        scope = "" if result.rating_format is None else f", {result.rating_format}"
        self.stdout.write(
            f"{heading}: as of {result.as_of.isoformat()}, spec version "
            f"{result.spec_version}{scope}."
        )
        self.stdout.write(
            f"Matches considered: {result.matches_considered}, counted: "
            f"{result.matches_counted}."
        )
        for kind in RecomputeChangeKind:
            self.stdout.write(f"Ratings {kind.value}: {result.count(kind)}")
        if not result.dry_run:
            self.stdout.write(f"Replay requests cleared: {result.requests_cleared}")
        if verbosity < 2:
            return
        # Counts per format only: a player's code next to a category
        # format would reveal their gender division.
        formats = sorted({change.rating_format for change in result.changes})
        for rating_format in formats:
            in_format = [c for c in result.changes if c.rating_format == rating_format]
            counts = ", ".join(
                f"{kind.value} {sum(1 for c in in_format if c.kind == kind)}"
                for kind in RecomputeChangeKind
            )
            self.stdout.write(f"{rating_format}: {counts}")
