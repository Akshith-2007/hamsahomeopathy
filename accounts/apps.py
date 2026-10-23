from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Users & Roles"

    def ready(self):
        from . import signals  # noqa: F401
