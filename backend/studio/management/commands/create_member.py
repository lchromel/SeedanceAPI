import getpass

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a non-staff team member with a validated password entered privately."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        User = get_user_model()
        if User.objects.filter(username=options["username"]).exists():
            raise CommandError("User already exists")
        user = User(username=options["username"])
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            raise CommandError("Passwords do not match")
        try:
            user.full_clean(exclude=["password"])
            password_validation.validate_password(password, user)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages))
        user.set_password(password)
        user.save()
        self.stdout.write("Member created. Enroll MFA before the first production login.")
