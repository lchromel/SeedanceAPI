from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from studio.models import Chunk
from studio.tasks import finish


class Command(BaseCommand):
    help = "Resolve an ambiguous submission only after checking the provider dashboard."

    def add_arguments(self, parser):
        parser.add_argument("chunk_id")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--provider-task-id")
        group.add_argument("--confirmed-not-created", action="store_true")

    def handle(self, *args, **options):
        with transaction.atomic():
            chunk = Chunk.objects.select_for_update(of=("self",)).get(id=options["chunk_id"])
            if chunk.state != "review":
                raise CommandError("Chunk is not awaiting review")
            if options["provider_task_id"]:
                chunk.provider_id = options["provider_task_id"]
                chunk.state, chunk.error = "generating", ""
                chunk.lease_until = chunk.next_poll = None
                chunk.save()
                chunk.run.state = "generating"
                chunk.run.save(update_fields=["state"])
            else:
                finish(
                    chunk.id,
                    "error",
                    message="Provider confirmed that no task was created.",
                )
                chunk.run.state = "error"
                chunk.run.save(update_fields=["state"])
        self.stdout.write("Reconciliation saved.")
