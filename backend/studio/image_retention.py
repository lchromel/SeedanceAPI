"""Lifecycle for temporary image bytes shared by Railway web and worker services."""

import io
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from . import storage
from .models import Asset, ImageGeneration

logger = logging.getLogger(__name__)


def available_assets(queryset):
    return queryset.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))


def reference_expiry():
    return timezone.now() + timedelta(seconds=settings.IMAGE_REFERENCE_TTL_SECONDS)


def prepare_references(job_id, assets):
    # Job-specific copies keep concurrent queued jobs independent of draft cleanup.
    keys = []
    copies = []
    try:
        for i, asset in enumerate(assets):
            if storage.is_temporary(asset.key):
                key = f"{asset.owner_id}/temporary-images/{job_id}/input-{i + 1}.jpg"
                copies.append(key)
                storage.put(key, io.BytesIO(storage.temporary_bytes(asset.key)), "image/jpeg")
                keys.append(key)
            else:
                keys.append(asset.key)
    except Exception:
        try:
            storage.delete_temporary(copies)
        except Exception:
            pass  # Every copy also has a Redis TTL, including after an interrupted request.
        raise
    return keys


def _release_references(job):
    assets = list(
        Asset.objects.filter(
            owner_id=job.owner_id,
            kind="image",
            id__in=[
                *job.snapshot["references"],
                *job.snapshot.get("analysis_reference_ids", []),
            ],
        )
    )
    temporary = [asset for asset in assets if storage.is_temporary(asset.key)]
    keys = list(job.snapshot.get("reference_keys", []))
    for asset in temporary:
        keys.extend([asset.key, asset.key.replace("/reference.jpg", "/preview.jpg")])
    try:
        storage.delete_temporary(keys)
    except Exception:
        # Do not turn a completed paid request into another submission. TTL remains active.
        logger.warning("Temporary reference cleanup deferred to TTL for image job %s", job.id)
    if temporary:
        Asset.objects.filter(
            owner_id=job.owner_id, pk__in=[asset.pk for asset in temporary]
        ).delete()


def release_references(job):
    try:
        _release_references(job)
    except Exception:
        logger.warning("Temporary reference cleanup deferred to TTL for image job %s", job.id)


def expire_records():
    now = timezone.now()
    Asset.objects.filter(kind="image", expires_at__lte=now).delete()
    ImageGeneration.objects.filter(state="ready", expires_at__lte=now).update(
        state="expired",
        output_key="",
        provider_output_url="",
        error="This temporary image has been deleted. Generate a new image to continue.",
    )
