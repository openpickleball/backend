"""The Player admin: read-only, with an audited reveal (issue #115, ADR 0046)
and an under-13 removal (issue #190, ADR 0059).

Staff with ``players.view_player`` see a Player's OPEN Code, display
name, city and region, and Account. The legal name, birth year and
month, and gender division show only as "Hidden": they are never listed,
searched, filtered, or sorted on. An Account that also has
``players.view_personal_data`` (no role holds it by default; the
Moderator group does not) sees a "Reveal personal data" button. Its POST
records one ``player.personal_data_view`` Audit Event, naming the fields
and never a value, and shows the values in that response only. A POST is
an admin write, so it needs a recent MFA step (ADR 0045). Nobody adds
or edits a Player here, and nobody deletes one except through the
under-13 removal below: Players change only through
``apps.players.services`` and ``apps.players.underage``.

An Account with ``players.remove_underage_player`` (no role holds it by
default) sees a "Remove personal data (under 13)" button (#190, ADR
0059). It leads to a confirmation page naming only the OPEN Code, whose
POST runs ``apps.players.underage.remove_underage_player``. That service
records the one Audit Event; this view records none.
"""

from typing import Any

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse

from apps.accounts.deletion import AccountAlreadyDeleted
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.players.models import Player
from apps.players.underage import (
    REMOVE_UNDERAGE_PERMISSION,
    UnderageRemovalRefused,
    remove_underage_player,
)

VIEW_PERSONAL_DATA_PERMISSION = "players.view_personal_data"
HIDDEN = "Hidden"
# The fields a reveal shows, as the Audit Event names them.
REVEALED_FIELDS = ("birth_month", "birth_year", "gender_division", "legal_name")


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin[Player]):
    """Read-only Players, except the under-13 removal (ADR 0059).

    Personal data only through the audited reveal.
    """

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
        context = {
            **(extra_context or {}),
            "may_reveal": self.may_reveal(request),
            "may_remove": self.may_remove(request),
        }
        return super().change_view(request, object_id, form_url, context)

    def may_remove(self, request: HttpRequest, player: Player | None = None) -> bool:
        return self.has_view_permission(request, player) and request.user.has_perm(
            REMOVE_UNDERAGE_PERMISSION
        )

    def get_urls(self) -> list[URLPattern]:
        name = f"{self.opts.app_label}_{self.opts.model_name}"
        reveal = path(
            "<path:object_id>/personal-data/",
            self.admin_site.admin_view(self.personal_data_view),
            name=f"{name}_personal_data",
        )
        remove = path(
            "<path:object_id>/remove-under-13/",
            self.admin_site.admin_view(self.remove_underage_view),
            name=f"{name}_remove_underage",
        )
        return [reveal, remove, *super().get_urls()]

    def remove_underage_view(
        self, request: HttpRequest, object_id: str
    ) -> HttpResponse:
        """GET confirms; POST removes the Player's personal data (ADR 0059).

        The service checks the permission again on the stored Account and
        records the one Audit Event. Every message names the OPEN Code only.
        """
        if request.method not in ("GET", "POST"):
            return HttpResponseNotAllowed(["GET", "POST"])
        if not self.may_remove(request):
            raise PermissionDenied
        player = self.get_object(request, unquote(object_id))
        if player is None:
            raise Http404
        if not self.may_remove(request, player):
            raise PermissionDenied
        change_url = reverse(
            f"admin:{self.opts.app_label}_{self.opts.model_name}_change",
            args=[player.pk],
            current_app=self.admin_site.name,
        )
        changelist_url = reverse(
            f"admin:{self.opts.app_label}_{self.opts.model_name}_changelist",
            current_app=self.admin_site.name,
        )
        if request.method == "GET":
            context = {
                **self.admin_site.each_context(request),
                "title": "Remove personal data (under 13)",
                "subtitle": player.public_code,
                "opts": self.opts,
                "original": player,
            }
            return TemplateResponse(
                request, "admin/players/player/remove_underage.html", context
            )
        code = player.public_code
        try:
            result = remove_underage_player(player, moderator=request.user)
        except (UnderageRemovalRefused, AccountAlreadyDeleted):
            self.message_user(
                request,
                f"The personal data of player {code} was not removed: "
                "this removal is not allowed or was already done.",
                messages.ERROR,
            )
            # Another removal may have deleted the Player meanwhile.
            if not Player.objects.filter(pk=player.pk).exists():
                return HttpResponseRedirect(changelist_url)
            return HttpResponseRedirect(change_url)
        self.message_user(
            request,
            f"The personal data of player {code} was removed.",
            messages.SUCCESS,
        )
        if result.player_kept:
            return HttpResponseRedirect(change_url)
        return HttpResponseRedirect(changelist_url)

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
