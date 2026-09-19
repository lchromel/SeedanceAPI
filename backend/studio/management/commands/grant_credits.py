from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from studio.billing import post


class Command(BaseCommand):
    help = "Grant credits with an idempotent, unique administrative reference."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("amount", type=int)
        parser.add_argument("--reference", required=True)

    def handle(self, *args, **options):
        if options["amount"] <= 0:
            raise CommandError("Amount must be positive")
        user = get_user_model().objects.get(username=options["username"])
        wallet = post(
            user.id,
            "grant",
            options["amount"],
            f"grant:{user.id}:{options['reference']}",
        )
        self.stdout.write(f"Available: {wallet.available}")
