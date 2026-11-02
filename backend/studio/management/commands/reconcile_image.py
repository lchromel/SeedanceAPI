from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.conf import settings
from urllib.parse import urlparse

from studio.image_tasks import finish
from studio.models import ImageGeneration


class Command(BaseCommand):
    help = "Resolve an uncertain Seedream submission after inspecting the provider dashboard."

    def add_arguments(self, parser):
        parser.add_argument("job_id")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--confirmed-not-created", action="store_true")
        group.add_argument("--output-url")

    @transaction.atomic
    def handle(self, *args, **options):
        job = ImageGeneration.objects.select_for_update().get(pk=options["job_id"])
        if job.state != "review":
            raise CommandError("This job is not awaiting review.")
        if options["output_url"]:
            url = urlparse(options["output_url"])
            if (
                url.scheme != "https"
                or url.username
                or url.password
                or not any(
                    url.hostname == host or (url.hostname or "").endswith("." + host)
                    for host in settings.OUTPUT_HOSTS
                )
            ):
                raise CommandError("Use a verified HTTPS result URL on an allowed provider host.")
            job.provider_output_url = options["output_url"]
            job.state, job.error, job.lease_until = "downloading", "", None
            job.save(update_fields=["provider_output_url", "state", "error", "lease_until"])
            self.stdout.write("Confirmed result saved. Its download will resume automatically.")
        else:
            finish(
                job.id,
                "error",
                error="Administrator confirmed the provider did not create an image.",
            )
            self.stdout.write("Reservation released. The user can submit a new generation.")
