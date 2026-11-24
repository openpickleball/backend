"""The moderator's match admin (#40).

Matches are read-only here: nothing is added, changed, or deleted through
a form. The default list is the matches that need review: disputed ones,
and ones an open flag links to a match that is not voided. Moderators
(``apps.matches.permissions.is_moderator``) get four actions, each through
``apps.matches.workflow``:

- approve: the result stands. A disputed match is resolved as confirmed,
  and the match's open flags are resolved as approved.
- void: a disputed match is resolved as voided; any other is voided.
- dismiss flags: the match's open flags are resolved as dismissed. Its
  status does not change.
- edit: opens a form for one match that calls ``edit_match``.

Every action needs a typed reason (``ReasonActionForm``), which goes into
its Audit Event. Pages and messages show matches and players by OPEN Code
only, never a name or an email.
"""

from typing import Any

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse

from apps.audit.admin_actions import (
    ReasonActionForm,
    action_reason,
    reason_problem,
)
from apps.matches.admin_forms import MatchEditForm, initial_values
from apps.matches.models import (
    FlagReason,
    FlagResolution,
    Match,
    MatchFlag,
    MatchStatus,
    Team,
)
from apps.matches.permissions import MatchActionDenied, is_moderator
from apps.matches.services import DuplicateMatchError, MatchValidationError
from apps.matches.workflow import (
    MatchStateError,
    approve_match,
    edit_match,
    resolve_dispute,
    resolve_flags,
    void_match,
)

BLANK_REASON = "Give a reason for this action."
SELECT_ONE = "Select exactly one match to edit."

_OPEN_FLAGS = MatchFlag.objects.filter(resolved_at__isnull=True)


def _open_flags_on() -> QuerySet[MatchFlag]:
    """Open flags whose other match (``duplicate_of``) is not voided."""
    return _OPEN_FLAGS.exclude(duplicate_of__status=MatchStatus.VOIDED)


def _open_flags_naming() -> QuerySet[MatchFlag]:
    """Open flags whose other match (``match``) is not voided."""
    return _OPEN_FLAGS.exclude(match__status=MatchStatus.VOIDED)


def needs_review(queryset: QuerySet[Match]) -> QuerySet[Match]:
    """Matches that are not voided and are disputed or held by an open flag."""
    held = Exists(_open_flags_on().filter(match=OuterRef("pk"))) | Exists(
        _open_flags_naming().filter(duplicate_of=OuterRef("pk"))
    )
    return queryset.exclude(status=MatchStatus.VOIDED).filter(
        Q(status=MatchStatus.DISPUTED) | held
    )


class ReviewFilter(admin.SimpleListFilter):
    """Needs review by default; "All matches" lists every match."""

    title = "review"
    parameter_name = "review"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[Any]
    ) -> list[tuple[str, str]]:
        return [("needs", "Needs review"), ("all", "All matches")]

    def choices(self, changelist: Any) -> Any:
        current = self.value() or "needs"
        for value, label in self.lookup_choices:
            yield {
                "selected": current == value,
                "query_string": changelist.get_query_string(
                    {self.parameter_name: value}
                ),
                "display": label,
            }

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        if self.value() == "all":
            return queryset
        return needs_review(queryset)


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
    """Read-only matches, with the moderator's review actions."""

    list_display = ("public_code", "status", "played_on", "format", "review")
    list_filter = (ReviewFilter, "status")
    search_fields = ("public_code",)
    ordering = ("-played_on", "-pk")
    fields = (
        "public_code",
        "status",
        "review",
        "format",
        "rating_format",
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
        "created_at",
    )
    readonly_fields = fields
    inlines = (FlagsOnMatchInline, FlagsNamingMatchInline)
    actions = (
        "approve_selected",
        "void_selected",
        "dismiss_flags_selected",
        "edit_selected",
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

    @admin.display(description="Review")
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

    @admin.action(permissions=["moderate"], description="Approve selected")
    def approve_selected(self, request: HttpRequest, queryset: QuerySet[Match]) -> None:
        self._apply(request, queryset, self._approve, "approved", "approve")

    @admin.action(permissions=["moderate"], description="Void selected")
    def void_selected(self, request: HttpRequest, queryset: QuerySet[Match]) -> None:
        self._apply(request, queryset, self._void, "voided", "void")

    @admin.action(permissions=["moderate"], description="Dismiss flags on selected")
    def dismiss_flags_selected(
        self, request: HttpRequest, queryset: QuerySet[Match]
    ) -> None:
        self._apply(request, queryset, self._dismiss, "dismissed", "dismiss")

    @admin.action(permissions=["moderate"], description="Edit selected match")
    def edit_selected(
        self, request: HttpRequest, queryset: QuerySet[Match]
    ) -> HttpResponse | None:
        pks = list(queryset.order_by("pk").values_list("pk", flat=True)[:2])
        if len(pks) != 1:
            self.message_user(request, SELECT_ONE, level=messages.WARNING)
            return None
        return HttpResponseRedirect(self._edit_url(pks[0]))

    @staticmethod
    def _approve(match: Match, request: HttpRequest, reason: str) -> bool:
        return approve_match(match, actor=request.user, reason=reason)

    @staticmethod
    def _void(match: Match, request: HttpRequest, reason: str) -> bool:
        current = Match.objects.filter(pk=match.pk).values_list("status", flat=True)
        if current.first() == MatchStatus.DISPUTED:
            resolve_dispute(
                match, actor=request.user, outcome=MatchStatus.VOIDED, reason=reason
            )
        else:
            void_match(match, actor=request.user, reason=reason)
        return True

    @staticmethod
    def _dismiss(match: Match, request: HttpRequest, reason: str) -> bool:
        return (
            resolve_flags(
                match,
                actor=request.user,
                outcome=FlagResolution.DISMISSED,
                reason=reason,
            )
            > 0
        )

    def _apply(
        self,
        request: HttpRequest,
        queryset: QuerySet[Match],
        change: Any,
        done: str,
        verb: str,
    ) -> None:
        reason = action_reason(request)
        problem = reason_problem(reason, BLANK_REASON)
        if problem is not None:
            self.message_user(request, problem, level=messages.WARNING)
            return
        count = 0
        # Which selected matches needed review before any change, so a
        # match an earlier one in this run released (both sides of one
        # flag) reads as reviewed, not as a failure.
        needed = set(needs_review(queryset).values_list("pk", flat=True))
        for match in queryset.order_by("pk"):
            # Each change in its own transaction: the workflow's advisory
            # locks last until the outermost transaction ends.
            try:
                changed = change(match, request, reason)
            except (MatchActionDenied, MatchStateError) as refused:
                self.message_user(
                    request,
                    f"{match.public_code} was not changed: {refused.args[0]}",
                    level=messages.WARNING,
                )
                continue
            if changed:
                count += 1
            elif (
                match.pk in needed
                and not needs_review(Match.objects.filter(pk=match.pk)).exists()
            ):
                self.message_user(request, f"{match.public_code} was already reviewed.")
            else:
                self.message_user(
                    request,
                    f"{match.public_code} was not changed: nothing to {verb}.",
                    level=messages.WARNING,
                )
        if count:
            self.message_user(request, f"{count} match(es) {done}.")

    # The edit page

    def get_urls(self) -> list[URLPattern]:
        edit = path(
            "<path:object_id>/edit/",
            self.admin_site.admin_view(self.edit_view),
            name="matches_match_edit",
        )
        return [edit, *super().get_urls()]

    def _edit_url(self, pk: object) -> str:
        return reverse(
            "admin:matches_match_edit", args=[pk], current_app=self.admin_site.name
        )

    def edit_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Correct one match through ``edit_match``. Moderators only."""
        if not is_moderator(request.user):
            raise PermissionDenied
        match = self.get_object(request, unquote(object_id))
        if match is None:
            raise Http404
        if request.method == "POST":
            form = MatchEditForm(request.POST)
            if form.is_valid():
                corrected = self._edit(request, match, form)
                if corrected is not None:
                    self.message_user(
                        request,
                        f"{match.public_code} was corrected as "
                        f"{corrected.public_code}.",
                    )
                    return HttpResponseRedirect(
                        reverse(
                            "admin:matches_match_change",
                            args=[corrected.pk],
                            current_app=self.admin_site.name,
                        )
                    )
        else:
            form = MatchEditForm(initial=initial_values(match))
        context = {
            **self.admin_site.each_context(request),
            "title": f"Edit match {match.public_code}",
            "opts": self.opts,
            "match": match,
            "form": form,
        }
        return TemplateResponse(request, "admin/matches/match/edit.html", context)

    @staticmethod
    def _edit(request: HttpRequest, match: Match, form: MatchEditForm) -> Match | None:
        data = form.cleaned_data
        same_day = data["played_on"] == match.played_on
        try:
            return edit_match(
                match,
                actor=request.user,
                reason=data["reason"],
                format=match.format,
                team_a=data["team_a"],
                team_b=data["team_b"],
                games=data["games"],
                played_on=data["played_on"],
                best_of=match.best_of,
                game_target=match.game_target,
                scoring_type=match.scoring_type,
                played_at=match.played_at if same_day else None,
                played_at_utc_offset=match.played_at_utc_offset if same_day else None,
                location=match.location,
                event_label=match.event_label,
                bracket_label=match.bracket_label,
                sequence=match.sequence,
            )
        except MatchValidationError as error:
            # The issues' messages are fixed text, never a submitted value.
            for message in dict.fromkeys(issue.message for issue in error.issues):
                form.add_error(None, message)
        except DuplicateMatchError as error:
            form.add_error(None, error.message)
        except (MatchStateError, MatchActionDenied) as error:
            form.add_error(None, str(error.args[0]))
        return None
