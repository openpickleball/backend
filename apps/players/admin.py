"""A read-only Player admin with an audited reveal (issue #115, ADR 0046).

Staff with ``players.view_player`` see a Player's OPEN Code, display
name, city and region, and Account. The legal name, birth year and
month, and gender division show only as "Hidden": they are never listed,
searched, filtered, or sorted on. An Account that also has
``players.view_personal_data`` (no role holds it by default; the
Moderator group does not) sees a "Reveal personal data" button. Its POST
records one ``player.personal_data_view`` Audit Event, naming the fields
and never a value, and shows the values in that response only. A POST is
an admin write, so it needs a recent MFA step (ADR 0045). Nobody adds,
changes, or deletes a Player here: Players change only through
``apps.players.services``.
"""

from typing import Any

from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.template.response import TemplateResponse
from django.urls import URLPattern, path

from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.players.models import Player

VIEW_PERSONAL_DATA_PERMISSION = "players.view_personal_data"
HIDDEN = "Hidden"
# The fields a reveal shows, as the Audit Event names them.
REVEALED_FIELDS = ("birth_month", "birth_year", "gender_division", "legal_name")


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin[Player]):
    """Read-only Players. Personal data only through the audited reveal."""

    list_display = ("public_code", "display_name")
    search_fields = ("public_code",)
    ordering = ("pk",)
    fields = (
        "public_code",
        "display_name",
        "account",
        "city",
        "region",
        "hidden_legal_name",
        "hidden_birth_year",
        "hidden_birth_month",
        "hidden_gender_division",
    )
    readonly_fields = fields

    def get_queryset(self, request: HttpRequest) -> QuerySet[Player]:
        # Never load the encrypted fields for the list or the detail page,
        # which show none of them: a row no configured key decrypts must
        # not break the page. The reveal reads them on access.
        return super().get_queryset(request).defer(*REVEALED_FIELDS)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    @admin.display(description="Legal name")
    def hidden_legal_name(self, player: Player) -> str:
        return HIDDEN

    @admin.display(description="Birth year")
    def hidden_birth_year(self, player: Player) -> str:
        return HIDDEN

    @admin.display(description="Birth month")
    def hidden_birth_month(self, player: Player) -> str:
        return HIDDEN

    @admin.display(description="Gender division")
    def hidden_gender_division(self, player: Player) -> str:
        return HIDDEN

    def may_reveal(self, request: HttpRequest, player: Player | None = None) -> bool:
        return self.has_view_permission(request, player) and request.user.has_perm(
            VIEW_PERSONAL_DATA_PERMISSION
        )

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        context = {**(extra_context or {}), "may_reveal": self.may_reveal(request)}
        return super().change_view(request, object_id, form_url, context)

    def get_urls(self) -> list[URLPattern]:
        reveal = path(
            "<path:object_id>/personal-data/",
            self.admin_site.admin_view(self.personal_data_view),
            name=f"{self.opts.app_label}_{self.opts.model_name}_personal_data",
        )
        return [reveal, *super().get_urls()]

    def personal_data_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Record one Audit Event, then show the four values in this response."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.may_reveal(request):
            raise PermissionDenied
        player = self.get_object(request, unquote(object_id))
        if player is None:
            raise Http404
        if not self.may_reveal(request, player):
            raise PermissionDenied
        with transaction.atomic():
            record_event(
                action=AuditAction.PLAYER_PERSONAL_DATA_VIEW,
                actor=request.user,
                target=player,
                after={"fields": list(REVEALED_FIELDS)},
            )
        context = {
            **self.admin_site.each_context(request),
            "title": "Personal data",
            "subtitle": player.public_code,
            "opts": self.opts,
            "original": player,
            "values": [
                ("Legal name", player.legal_name),
                ("Birth year", player.birth_year),
                ("Birth month", player.birth_month),
                ("Gender division", player.get_gender_division_display()),
            ],
        }
        return TemplateResponse(
            request, "admin/players/player/personal_data.html", context
        )
