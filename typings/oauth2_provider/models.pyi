# AbstractApplication, so the django-stubs plugin sees the swapped
# Application model (partners.PartnerApplication) as a model, and the
# model getters OPEN calls.
import datetime as dt
from typing import Any, ClassVar

from django.db import models
from django.db.models.expressions import Combinable

_Str = models.CharField[str | int | Combinable, str]
_Text = models.TextField[str | Combinable, str]
_Bool = models.BooleanField[bool | Combinable, bool]

class ClientSecretField(models.CharField[str | int | Combinable, str]): ...

class AbstractApplication(models.Model):
    CLIENT_CONFIDENTIAL: ClassVar[str]
    CLIENT_PUBLIC: ClassVar[str]
    GRANT_AUTHORIZATION_CODE: ClassVar[str]
    GRANT_DEVICE_CODE: ClassVar[str]
    GRANT_IMPLICIT: ClassVar[str]
    GRANT_PASSWORD: ClassVar[str]
    GRANT_CLIENT_CREDENTIALS: ClassVar[str]
    GRANT_OPENID_HYBRID: ClassVar[str]
    NO_ALGORITHM: ClassVar[str]
    RS256_ALGORITHM: ClassVar[str]
    HS256_ALGORITHM: ClassVar[str]

    class Meta:
        abstract: ClassVar[bool]

    id: models.BigAutoField[str | int | Combinable, int]
    client_id: _Str
    user: models.ForeignKey[Any, Any]
    redirect_uris: _Text
    post_logout_redirect_uris: _Text
    client_type: _Str
    authorization_grant_type: _Str
    client_secret: ClientSecretField
    hash_client_secret: _Bool
    name: _Str
    skip_authorization: _Bool
    created: models.DateTimeField[str | dt.datetime | Combinable, dt.datetime]
    updated: models.DateTimeField[str | dt.datetime | Combinable, dt.datetime]
    algorithm: _Str
    allowed_origins: _Text
    registration_source: _Str
    cimd_expires_at: models.DateTimeField[
        str | dt.datetime | Combinable | None, dt.datetime | None
    ]

    def clean(self) -> None: ...

def get_application_model() -> type[AbstractApplication]: ...

# The token models are not stubbed, so their model class is untyped.
def get_access_token_model() -> Any: ...
def get_grant_model() -> Any: ...
def get_id_token_model() -> Any: ...
def get_refresh_token_model() -> Any: ...
def get_device_grant_model() -> Any: ...
