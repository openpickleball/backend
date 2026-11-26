"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.accounts.headless_views import headless_urlpatterns
from config.api import api

urlpatterns = [
    # A private path in production, admin/ only locally (#101).
    path(settings.ADMIN_URL, admin.site.urls),
    path("api/v1/", api.urls),
    # Only the token and revoke views (apps/partners/urls.py).
    path("oauth/", include("apps.partners.urls")),
    # OPEN's replacements for some allauth headless views, at allauth's own
    # paths, so they must come first (apps/accounts/headless_views.py).
    path("_allauth/", include(headless_urlpatterns())),
    # allauth's headless JSON API (sign-up, login, email verification,
    # password reset) for the browser and app clients, and its OpenAPI
    # document. allauth.urls (server-rendered pages) is never mounted.
    path("_allauth/", include("allauth.headless.urls")),
]
