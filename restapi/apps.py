from django.apps import AppConfig


class ApiConfig(AppConfig):
    # Named "restapi" (not "api") because hosts like Vercel reserve a top-level api/ folder.
    name = "restapi"
    verbose_name = "REST API"
