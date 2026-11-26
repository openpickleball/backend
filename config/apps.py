"""Django app configurations for the ``config`` project package."""

from django.contrib.admin.apps import AdminConfig


class OpenAdminConfig(AdminConfig):
    """django.contrib.admin, with OPEN's admin site as ``admin.site``
    (``config/admin.py``: every staff Account needs MFA, ADR 0043)."""

    default_site = "config.admin.OpenAdminSite"
