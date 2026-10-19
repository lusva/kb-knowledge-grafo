import os
import sys

from django.apps import AppConfig


class KbConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "kb"

    def ready(self):
        """Aggiornamento delle sole cartelle documentali, durante il server."""
        command = sys.argv[1] if len(sys.argv) > 1 else ""
        if command not in {"runserver", "runserver_plus"}:
            return
        if "--noreload" not in sys.argv and not os.environ.get("RUN_MAIN"):
            return
        from . import autoindex
        autoindex.avvia()
