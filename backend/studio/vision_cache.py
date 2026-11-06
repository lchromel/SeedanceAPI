"""Private, source-bound observation reuse with short database leases across workers."""

import hashlib
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import ImageObservation

VERSION = 1


def key(asset, focus, detailed, original=None):
    payload = [
        VERSION,
        str(asset.pk),
        asset.key,
        settings.REFERENCE_VISION_MODEL,
        focus,
        detailed,
        [str(original.pk), original.key] if original else None,
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def acquire(asset, cache_key):
    row, _ = ImageObservation.objects.get_or_create(asset=asset, cache_key=cache_key)
    if row.description:
        return row.description, None
    token = uuid.uuid4()
    claimed = (
        ImageObservation.objects.filter(pk=row.pk, description="")
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
        .update(claim=token, lease_until=timezone.now() + timedelta(seconds=80))
    )
    return None, token if claimed else None


def save(cache_key, token, description):
    ImageObservation.objects.filter(cache_key=cache_key, claim=token).update(
        description=description, claim=None, lease_until=None
    )


def release(cache_key, token):
    ImageObservation.objects.filter(cache_key=cache_key, claim=token).update(
        claim=None, lease_until=None
    )
