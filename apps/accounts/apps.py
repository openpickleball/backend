from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    verbose_name = "Accounts"

    def ready(self) -> None:
        from django.contrib.auth.signals import user_logged_out

        from apps.accounts.receivers import forget_session_on_logout

        user_logged_out.connect(
            forget_session_on_logout, dispatch_uid="apps.accounts.forget_session"
        )
