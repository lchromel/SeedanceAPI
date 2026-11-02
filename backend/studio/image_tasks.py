"""One paid submission per durable image job; downloads can safely be retried."""

import tempfile
from datetime import timedelta
from pathlib import Path

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from PIL import Image

from . import provider, storage
from .billing import post
from .models import ImageGeneration


def submit(job):
    payload = {
        "model": job.snapshot["model"],
        "prompt": job.snapshot["prompt"],
        "size": job.snapshot["size"],
        "output_format": "jpeg",
        "response_format": "url",
        "watermark": False,
    }
    if job.snapshot["reference_keys"]:
        payload["image"] = [
            storage.url(key, provider=True) for key in job.snapshot["reference_keys"]
        ]
    response = requests.post(
        settings.ARK_BASE_URL + "/images/generations",
        json=payload,
        headers={"Authorization": f"Bearer {settings.ARK_API_KEY}"},
        timeout=(10, 180),
        allow_redirects=False,
    )
    if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
        raise provider.Rejected("Seedream rejected this request. Check your prompt and references.")
    response.raise_for_status()
    items = response.json().get("data")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Submission outcome is unknown")
    item = items[0]
    if item.get("error"):
        raise provider.Rejected("Seedream could not generate this image. Revise the prompt.")
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Submission outcome is unknown")
    return url


@transaction.atomic
def finish(job_id, state, key="", error=""):
    job = ImageGeneration.objects.select_for_update().get(pk=job_id)
    if job.state in ("ready", "error"):
        return
    post(
        job.owner_id, "settle" if state == "ready" else "release", job.cost, f"image:{job.id}:final"
    )
    job.state, job.output_key, job.error, job.lease_until = state, key, error, None
    job.provider_output_url = ""
    job.save()


@shared_task
def process_image(job_id):
    now = timezone.now()
    with transaction.atomic():
        job = ImageGeneration.objects.select_for_update().get(pk=job_id)
        if job.state not in ("queued", "downloading") or (
            job.lease_until and job.lease_until > now
        ):
            return
        submitting = job.state == "queued"
        job.state = "generating" if submitting else "downloading"
        job.lease_until = now + timedelta(minutes=5)
        job.save(update_fields=["state", "lease_until"])
    if job.snapshot["provider"] == "mock":
        if not settings.DEBUG:
            finish(job.id, "error", error="Simulation is unavailable in production.")
            return
    elif submitting:
        try:
            job.provider_output_url = submit(job)
        except provider.Rejected as exc:
            finish(job.id, "error", error=str(exc))
            return
        except Exception:
            ImageGeneration.objects.filter(pk=job.id).update(
                state="review",
                lease_until=None,
                error="Submission outcome needs administrator review. Credits remain reserved.",
            )
            return
        # Persist the URL before downloading. A worker restart must never resubmit.
        ImageGeneration.objects.filter(pk=job.id).update(
            state="downloading",
            provider_output_url=job.provider_output_url,
        )
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "output.jpg"
            if job.snapshot["provider"] == "mock":
                width, height = map(int, job.snapshot["size"].split("x"))
                Image.new("RGB", (width, height), "#292b2f").save(path, "JPEG")
            else:
                provider.download(job.provider_output_url, path, max_bytes=30 * 1024 * 1024)
            with Image.open(path) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError("Output exceeds image limits")
                image.load()
                clean = image.convert("RGB")
            clean.save(path, "JPEG", quality=95)
            key = f"{job.owner_id}/images/{job.id}.jpg"
            with path.open("rb") as stream:
                storage.put(key, stream, "image/jpeg")
        finish(job.id, "ready", key)
    except Exception:
        # The paid result is known; retry only its download/storage, never generation.
        ImageGeneration.objects.filter(pk=job.id).update(
            state="downloading",
            lease_until=timezone.now() + timedelta(seconds=60),
            error="Saving the generated image. Retrying automatically.",
        )
