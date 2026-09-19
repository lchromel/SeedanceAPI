import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from studio.models import Chunk, Run
from studio.tasks import assemble, process_chunk


class Command(BaseCommand):
    help = "Execute local mock jobs without Redis. Never runs paid jobs."

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")

    def handle(self, *args, **options):
        if not settings.DEBUG or settings.GENERATION_PROVIDER != "mock":
            raise CommandError("Local simulation only")
        while True:
            for chunk in Chunk.objects.filter(state="queued"):
                process_chunk(str(chunk.id))
            for run in Run.objects.filter(state__in=["generating", "assembling"]):
                if not run.chunks.exclude(state="ready").exists():
                    assemble(str(run.id))
            if not options["watch"]:
                return
            close_old_connections()
            time.sleep(2)
