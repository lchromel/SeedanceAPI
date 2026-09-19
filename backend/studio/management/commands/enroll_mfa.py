import pyotp
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from studio.models import MfaDevice


class Command(BaseCommand):
    help = "Provision an authenticator for a team member through a trusted terminal."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        if not settings.MFA_ENCRYPTION_KEY:
            raise CommandError("Set MFA_ENCRYPTION_KEY first")
        user = get_user_model().objects.get(username=options["username"])
        if MfaDevice.objects.filter(user=user).exists():
            raise CommandError("An authenticator already exists; do not silently replace it.")
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        self.stdout.write(
            "Open this enrollment URI privately in your authenticator. Do not put it in logs or chat:"
        )
        self.stdout.write(totp.provisioning_uri(user.username, issuer_name="Video Studio"))
        code = input("Enter the current verification code to confirm enrollment: ").strip()
        if not totp.verify(code, valid_window=1):
            raise CommandError("Verification failed; nothing saved")
        MfaDevice.objects.create(
            user=user,
            encrypted_secret=Fernet(settings.MFA_ENCRYPTION_KEY.encode())
            .encrypt(secret.encode())
            .decode(),
        )
        self.stdout.write("Authenticator enrolled.")
