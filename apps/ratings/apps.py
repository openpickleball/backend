from django.apps import AppConfig


class RatingsConfig(AppConfig):
    name = "apps.ratings"
    label = "ratings"
    verbose_name = "Ratings"

    def ready(self) -> None:
        # Connects the match_confirmed receiver that queues live updates.
        from apps.ratings import receivers  # noqa: F401
