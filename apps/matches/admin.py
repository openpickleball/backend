"""The moderator's match admin (#40, #273).

Matches are read-only here: nothing is added, changed, or deleted through
a form, and moderators do not review single matches (#273, ADR 0034).
The list shows every match by default; the "Disputed or held" filter
narrows it to disputed matches and ones an open flag links to a match
that is not voided. Moderators
(``apps.matches.permissions.is_moderator``) get two actions, each through
``apps.matches.workflow``:

- void upload batch: for one selected match, voids every match of its
  Submission Batch (``void_submission_batch``).
- void submitter since: for one selected match, voids every match its
  submitter recorded on or after the day (UTC) the selected match was
  recorded (``void_submitter_matches``).

Both first show a confirmation page with how many matches the run would
void and how many are already voided, and change nothing until the
moderator confirms. Each needs a typed reason (``ReasonActionForm``),
which goes into its Audit Events. A match whose correction chain holds
the moderator's own Player is counted as refused. Pages and messages show
matches and players by OPEN Code only, never a name or an email.
"""

from datetime import UTC
from typing import Any

from django.contrib import admin, messages
from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse

from apps.audit.admin_actions import (
    ReasonActionForm,
    action_reason,
    reason_problem,
)
from apps.audit.admin_audit import audited_by_service
from apps.matches.models import (
    FlagReason,
    Match,
    MatchFlag,
    MatchStatus,
    SubmissionBatch,
    Team,
)
from apps.matches.permissions import MatchActionDenied, is_moderator
from apps.matches.workflow import (
    BulkVoidResult,
    submission_batch_matches,
    submitter_matches,
    void_submission_batch,
    void_submitter_matches,
)

BLANK_REASON = "Give a reason for this action."
BULK_SELECT_ONE = "Select exactly one match to void in bulk."
NO_BATCH = "The selected match did not come in an upload batch."
BULK_CONFIRMATION = "admin/matches/match/bulk_void_confirmation.html"

_OPEN_FLAGS = MatchFlag.objects.filter(resolved_at__isnull=True)


def _open_flags_on() -> QuerySet[MatchFlag]:
    """Open flags whose other match (``duplicate_of``) is not voided."""
    return _OPEN_FLAGS.exclude(duplicate_of__status=MatchStatus.VOIDED)


def _open_flags_naming() -> QuerySet[MatchFlag]:
    """Open flags whose other match (``match``) is not voided."""
    return _OPEN_FLAGS.exclude(match__status=MatchStatus.VOIDED)


def disputed_or_held(queryset: QuerySet[Match]) -> QuerySet[Match]:
    """Matches that are not voided and are disputed or held by an open flag."""
    held = Exists(_open_flags_on().filter(match=OuterRef("pk"))) | Exists(
        _open_flags_naming().filter(duplicate_of=OuterRef("pk"))
    )
    return queryset.exclude(status=MatchStatus.VOIDED).filter(
        Q(status=MatchStatus.DISPUTED) | held
    )


class DisputedOrHeldFilter(admin.SimpleListFilter):
    """Every match by default; "Disputed or held" narrows the list."""

    title = "disputed or held"
    parameter_name = "held"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[Any]
    ) -> list[tuple[str, str]]:
        return [("yes", "Disputed or held")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        if self.value() == "yes":
            return disputed_or_held(queryset)
        return queryset


class _FlagInline(admin.TabularInline[MatchFlag, Match]):
    model = MatchFlag
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class FlagsOnMatchInline(_FlagInline):
    fk_name = "match"
    fields = ("reason", "duplicate_of", "created_at", "resolution", "resolved_at")
    readonly_fields = fields
    verbose_name = "flag on this match"
    verbose_name_plural = "flags on this match"


class FlagsNamingMatchInline(_FlagInline):
    fk_name = "duplicate_of"
    fields = ("reason", "match", "created_at", "resolution", "resolved_at")
    readonly_fields = fields
    verbose_name = "flag naming this match"
    verbose_name_plural = "flags naming this match as the earlier one"


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin[Match]):
    """Read-only matches, with the moderator's bulk void actions."""

    list_display = ("public_code", "status", "played_on", "format", "review")
    list_filter = (DisputedOrHeldFilter, "status")
    search_fields = ("public_code",)
    ordering = ("-played_on", "-pk")
    fields = (
        "public_code",
        "status",
        "review",
        "format",
        "rating_format",
        "match_type",
        "played_on",
        "played_at",
        "played_at_utc_offset",
        "best_of",
        "game_target",
        "scoring_type",
        "teams",
        "scores",
        "submitted_by",
        "event",
        "club",
        "corrects",
        "location",
        "event_label",
        "bracket_label",
        "sequence",
        "submission_batch",
        "created_at",
    )
    readonly_fields = fields
    inlines = (FlagsOnMatchInline, FlagsNamingMatchInline)
    actions = (
        "void_batch_selected",
        "void_submitter_selected",
    )
    action_form = ReasonActionForm

    def get_queryset(self, request: HttpRequest) -> QuerySet[Match]:
        return (
            super()
            .get_queryset(request)
            .prefetch_related(
                Prefetch("flags", queryset=_open_flags_on(), to_attr="open_flags"),
                Prefetch(
                    "duplicate_flags",
                    queryset=_open_flags_naming(),
                    to_attr="open_flags_naming",
                ),
            )
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_moderate_permission(self, request: HttpRequest) -> bool:
        """Django shows an action marked ``permissions=["moderate"]`` only
        when this is True.
        """
        return is_moderator(request.user)

    # Computed fields

    @admin.display(description="Disputed or held")
    def review(self, match: Match) -> str:
        parts: list[str] = []
        if match.status == MatchStatus.DISPUTED:
            parts.append("Disputed")
        flags = [
            *getattr(match, "open_flags", []),
            *getattr(match, "open_flags_naming", []),
        ]
        reasons = sorted({FlagReason(flag.reason).label for flag in flags})
        parts.extend(str(reason) for reason in reasons)
        return "; ".join(parts) or "-"

    @admin.display(description="Teams")
    def teams(self, match: Match) -> str:
        participants = match.participants.select_related("player").order_by(
            "team", "pk"
        )
        codes: dict[str, list[str]] = {Team.A: [], Team.B: []}
        for participant in participants:
            codes[participant.team].append(participant.player.public_code)
        return f"A: {', '.join(codes[Team.A])}; B: {', '.join(codes[Team.B])}"

    @admin.display(description="Scores")
    def scores(self, match: Match) -> str:
        games = match.games.order_by("game_number")
        return ", ".join(f"{g.team_a_score}-{g.team_b_score}" for g in games) or "-"

    @admin.display(description="Submitted by")
    def submitted_by(self, match: Match) -> str:
        # Never the Account itself: its __str__ is the email.
        if match.submitted_by_user_id is not None:
            return "Account"
        application = match.submitted_by_application
        return application.client_id if application is not None else "-"

    # Actions

    # The bulk void services record their own events, and must never run
    # inside an outer transaction (#101).
    @audited_by_service
    @admin.action(
        permissions=["moderate"],
        description="Void every match in the selected match's upload batch",
    )
    def void_batch_selected(
        self, request: HttpRequest, queryset: QuerySet[Match]
    ) -> HttpResponse | None:
        return self._bulk(request, queryset, "batch")

    @audited_by_service
    @admin.action(
        permissions=["moderate"],
        description=(
            "Void every match its submitter recorded on or after the selected "
            "match's day (UTC)"
        ),
    )
    def void_submitter_selected(
        self, request: HttpRequest, queryset: QuerySet[Match]
    ) -> HttpResponse | None:
        return self._bulk(request, queryset, "submitter")

    def _bulk(
        self, request: HttpRequest, queryset: QuerySet[Match], scope: str
    ) -> HttpResponse | None:
        """Show the bulk void confirmation page, or run it on ``post=yes``.

        Both POSTs check the reason and the single selection again. The
        page shows the selected match's OPEN Code, the scope (the batch,
        or the submitter as "Account" or its application's ``client_id``,
        with the date), the counts, and the reason, never a name or email.
        """
        reason = action_reason(request)
        problem = reason_problem(reason, BLANK_REASON)
        if problem is not None:
            self.message_user(request, problem, level=messages.WARNING)
            return None
        pks = list(queryset.order_by("pk").values_list("pk", flat=True)[:2])
        if len(pks) != 1:
            self.message_user(request, BULK_SELECT_ONE, level=messages.WARNING)
            return None
        match = Match.objects.select_related(
            "submission_batch", "submitted_by_user", "submitted_by_application"
        ).get(pk=pks[0])
        batch: SubmissionBatch | None = match.submission_batch
        recorded_since = match.created_at.astimezone(UTC).date()
        if scope == "batch":
            if batch is None:
                self.message_user(request, NO_BATCH, level=messages.WARNING)
                return None
            matches = submission_batch_matches(batch)
        else:
            submitter = match.submitted_by_user or match.submitted_by_application
            assert submitter is not None  # one submitter, a database rule
            matches = submitter_matches(submitter, recorded_since)
        if request.POST.get("post") != "yes":
            context = {
                **self.admin_site.each_context(request),
                "title": "Void matches in bulk",
                "opts": self.opts,
                "match": match,
                "scope": scope,
                "batch": batch,
                "submitter": self.submitted_by(match),
                "recorded_since": recorded_since.isoformat(),
                "to_void": matches.exclude(status=MatchStatus.VOIDED).count(),
                "already_voided": matches.filter(status=MatchStatus.VOIDED).count(),
                "reason": reason,
                "action": request.POST.get("action", ""),
                "selected": match.pk,
            }
            return TemplateResponse(request, BULK_CONFIRMATION, context)
        try:
            result = self._run_bulk(request, match, scope, reason)
        except MatchActionDenied:
            self.message_user(
                request, MatchActionDenied.message, level=messages.WARNING
            )
            return None
        self.message_user(
            request,
            f"{result.voided} match(es) voided, {result.skipped} already voided, "
            f"{result.refused} refused.",
        )
        return None

    @staticmethod
    def _run_bulk(
        request: HttpRequest, match: Match, scope: str, reason: str
    ) -> BulkVoidResult:
        if scope == "batch":
            assert match.submission_batch is not None
            return void_submission_batch(
                match.submission_batch, actor=request.user, reason=reason
            )
        submitter = match.submitted_by_user or match.submitted_by_application
        assert submitter is not None
        return void_submitter_matches(
            submitter,
            recorded_since=match.created_at.astimezone(UTC).date(),
            actor=request.user,
            reason=reason,
        )
