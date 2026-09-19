import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Asset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(
        max_length=16,
        choices=[(x, x) for x in ["character", "clothing", "location", "motion"]],
    )
    name = models.CharField(max_length=120)
    key = models.CharField(max_length=255)
    mime = models.CharField(max_length=64)
    duration = models.PositiveIntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=120, default="New video")
    config = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    updated = models.DateTimeField(auto_now=True)


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="runs")
    part = models.CharField(max_length=16)
    snapshot = models.JSONField()
    request_key = models.UUIDField()
    state = models.CharField(max_length=20, default="generating")
    output_key = models.CharField(max_length=255, blank=True)
    assembly_lease = models.DateTimeField(null=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "request_key"], name="unique_generation_request"
            )
        ]


class Chunk(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField()
    start = models.PositiveIntegerField()
    duration = models.PositiveIntegerField()
    prompt = models.TextField()
    motion_asset = models.ForeignKey(Asset, null=True, on_delete=models.PROTECT)
    state = models.CharField(max_length=20, default="queued")
    provider_id = models.CharField(max_length=255, blank=True)
    output_key = models.CharField(max_length=255, blank=True)
    error = models.CharField(max_length=255, blank=True)
    cost = models.PositiveIntegerField()
    attempt = models.PositiveIntegerField(default=1)
    lease_until = models.DateTimeField(null=True)
    next_poll = models.DateTimeField(null=True)
    started = models.DateTimeField(null=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["run", "position"], name="unique_chunk_position")
        ]


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, primary_key=True, on_delete=models.CASCADE
    )
    available = models.BigIntegerField(default=0)
    held = models.BigIntegerField(default=0)
    spent = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(available__gte=0, held__gte=0, spent__gte=0),
                name="nonnegative_wallet",
            )
        ]


class Ledger(models.Model):
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="entries")
    key = models.CharField(max_length=150, unique=True)
    kind = models.CharField(max_length=16)
    amount = models.PositiveIntegerField()
    created = models.DateTimeField(auto_now_add=True)


class MfaDevice(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    encrypted_secret = models.TextField()
    last_counter = models.BigIntegerField(default=-1)


class LoginBucket(models.Model):
    key = models.CharField(primary_key=True, max_length=64)
    count = models.PositiveIntegerField(default=0)
    window = models.DateTimeField()
