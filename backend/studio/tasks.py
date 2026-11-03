import logging
import tempfile
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import image_retention, media, provider, storage
from .billing import post
from .models import Chunk, LoginBucket, Run
from .image_tasks import process_image

logger = logging.getLogger(__name__)


@shared_task
def dispatch():
    now = timezone.now()
    image_retention.expire_records()
    LoginBucket.objects.filter(window__lt=now - timedelta(days=1)).delete()
    from .models import ImageGeneration

    ImageGeneration.objects.filter(state="generating", lease_until__lt=now).update(
        state="review", error="Submission needs administrator review. No automatic resubmission.",
        lease_until=None,
    )
    for image_id in (
        ImageGeneration.objects.filter(state__in=["queued", "downloading"])
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
        .values_list("id", flat=True)[:100]
    ):
        process_image.delay(str(image_id))
    # Durable pending rows survive broker outages and worker restarts.
    for chunk_id in (
        Chunk.objects.filter(cleanup_state="pending", state="ready")
        .filter(Q(cleanup_lease_until__isnull=True) | Q(cleanup_lease_until__lt=now))
        .filter(Q(cleanup_next_attempt__isnull=True) | Q(cleanup_next_attempt__lte=now))
        .order_by("cleanup_next_attempt", "id")
        .values_list("id", flat=True)[:100]
    ):
        cleanup_provider_task.delay(str(chunk_id))
    # A worker lost during submission may already have incurred a provider charge.
    Chunk.objects.filter(state="submitting", lease_until__lt=now).update(
        state="review",
        error="Submission needs reconciliation. No automatic resubmission.",
    )
    for chunk_id in (
        Chunk.objects.filter(state__in=["queued", "generating"])
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
        .filter(Q(next_poll__isnull=True) | Q(next_poll__lte=now))
        .values_list("id", flat=True)[:100]
    ):
        process_chunk.delay(str(chunk_id))
    for run in Run.objects.filter(state__in=["generating", "assembling"]):
        states = set(run.chunks.values_list("state", flat=True))
        if states == {"ready"} and (run.assembly_lease is None or run.assembly_lease < now):
            assemble.delay(str(run.id))
        elif "review" in states:
            Run.objects.filter(id=run.id).update(state="review")
        elif states <= {"ready", "error"} and "error" in states:
            Run.objects.filter(id=run.id).update(state="error")


def finish(chunk_id, state, key="", message=""):
    with transaction.atomic():
        chunk = (
            Chunk.objects.select_for_update(of=("self",))
            .select_related("run__project")
            .get(id=chunk_id)
        )
        if chunk.state in ("ready", "error"):
            return
        # Finish earlier reservations by their stored cost, even if billing was paused.
        if chunk.cost:
            post(
                chunk.run.project.owner_id,
                "settle" if state == "ready" else "release",
                chunk.cost,
                f"{chunk.id}:{chunk.attempt}:final",
            )
        chunk.state, chunk.output_key, chunk.error = state, key, message
        chunk.lease_until = None
        if (
            state == "ready" and key and chunk.provider_id
            and chunk.run.snapshot.get("provider") == "byteplus"
        ):
            # Commit the deletion intent atomically with the stored output and billing.
            chunk.cleanup_state = "pending"
            chunk.cleanup_next_attempt = timezone.now()
        chunk.save()


@shared_task
def cleanup_provider_task(chunk_id):
    now = timezone.now()
    with transaction.atomic():
        chunk = (
            Chunk.objects.select_for_update(of=("self",))
            .select_related("run").get(id=chunk_id)
        )
        if (
            chunk.cleanup_state != "pending" or chunk.state != "ready"
            or not chunk.output_key or not chunk.provider_id
            or chunk.run.snapshot.get("provider") != "byteplus"
            or (chunk.cleanup_lease_until and chunk.cleanup_lease_until > now)
            or (chunk.cleanup_next_attempt and chunk.cleanup_next_attempt > now)
        ):
            return
        chunk.cleanup_attempts += 1
        chunk.cleanup_lease_until = now + timedelta(minutes=2)
        chunk.save(update_fields=["cleanup_attempts", "cleanup_lease_until"])
    try:
        provider.delete_task(chunk.provider_id)
    except Exception:
        # Never put provider response bodies, URLs or credentials in stored errors.
        logger.warning("Provider cleanup failed for chunk %s; retry scheduled", chunk.id)
        Chunk.objects.filter(id=chunk.id, cleanup_state="pending").update(
            cleanup_lease_until=None,
            cleanup_next_attempt=timezone.now() + timedelta(
                seconds=min(3600, 60 * 2 ** min(chunk.cleanup_attempts - 1, 6))
            ),
            cleanup_error="Provider deletion failed; retry scheduled.",
        )
        return
    Chunk.objects.filter(id=chunk.id, cleanup_state="pending").update(
        cleanup_state="deleted", provider_deleted_at=timezone.now(),
        cleanup_lease_until=None, cleanup_next_attempt=None, cleanup_error="",
    )


@shared_task
def process_chunk(chunk_id):
    now = timezone.now()
    with transaction.atomic():
        chunk = (
            Chunk.objects.select_for_update(of=("self",))
            .select_related("run__project", "motion_asset")
            .get(id=chunk_id)
        )
        if chunk.state not in ("queued", "generating") or (
            chunk.lease_until and chunk.lease_until > now
        ):
            return
        queued = chunk.state == "queued"
        chunk.lease_until = now + timedelta(minutes=12)
        chunk.started = chunk.started or now
        chunk.save(update_fields=["lease_until", "started"])
    try:
        if chunk.run.snapshot["provider"] == "mock":
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "clip.mp4"
                media.mock_clip(output, chunk.duration)
                key = f"{chunk.run.project.owner_id}/outputs/{chunk.id}-{chunk.attempt}.mp4"
                with output.open("rb") as stream:
                    storage.put(key, stream, "video/mp4")
                finish(chunk.id, "ready", key)
            return
        if queued:
            data = provider.payload(chunk)  # Local preparation can be retried safely.
            Chunk.objects.filter(id=chunk.id).update(state="submitting")
            try:
                task_id = provider.submit(data)
            except provider.Rejected as exc:
                finish(chunk.id, "error", message=str(exc))
                return
            except Exception:
                Chunk.objects.filter(id=chunk.id).update(
                    state="review",
                    error="Provider submission is unconfirmed. Contact an administrator.",
                    lease_until=None,
                )
                return
            Chunk.objects.filter(id=chunk.id).update(
                provider_id=task_id,
                state="generating",
                lease_until=None,
                next_poll=now + timedelta(seconds=15),
            )
            return
        status, output_url = provider.status(chunk.provider_id)
        if status in ("failed", "cancelled", "expired"):
            finish(chunk.id, "error", message="The provider could not generate this clip.")
        elif status == "succeeded" and output_url:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "clip.mp4"
                provider.download(output_url, path)
                media.probe(path)
                key = f"{chunk.run.project.owner_id}/outputs/{chunk.id}-{chunk.attempt}.mp4"
                with path.open("rb") as stream:
                    storage.put(key, stream, "video/mp4")
                finish(chunk.id, "ready", key)
        else:
            if now - chunk.started > timedelta(hours=24):
                Chunk.objects.filter(id=chunk.id).update(
                    state="review",
                    error="Provider task needs administrator review.",
                )
            Chunk.objects.filter(id=chunk.id).update(
                lease_until=None, next_poll=now + timedelta(seconds=15)
            )
    except Exception:
        if now - chunk.started > timedelta(hours=24):
            Chunk.objects.filter(id=chunk.id).update(
                state="review",
                error="The task needs administrator review.",
                lease_until=None,
            )
            return
        # Polling/download/preparation failures never submit another paid task.
        Chunk.objects.filter(id=chunk.id).update(
            lease_until=None,
            next_poll=now + timedelta(seconds=60),
            error="Temporarily unavailable; checking again.",
        )


@shared_task
def assemble(run_id):
    now = timezone.now()
    with transaction.atomic():
        run = Run.objects.select_for_update(of=("self",)).select_related("project").get(id=run_id)
        if (
            run.output_key
            or run.chunks.exclude(state="ready").exists()
            or (run.assembly_lease and run.assembly_lease > now)
        ):
            return
        run.state, run.assembly_lease = "assembling", now + timedelta(minutes=12)
        run.save(update_fields=["state", "assembly_lease"])
    try:
        chunks = list(run.chunks.all())
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            paths = []
            for index, chunk in enumerate(chunks):
                path = folder / f"input-{index}.mp4"
                storage.fetch(chunk.output_key, path)
                paths.append(path)
            result = media.assemble_files(paths, [c.duration for c in chunks], folder)
            key = f"{run.project.owner_id}/outputs/{run.id}.mp4"
            with result.open("rb") as stream:
                storage.put(key, stream, "video/mp4")
            Run.objects.filter(id=run.id).update(state="ready", output_key=key, assembly_lease=None)
    except Exception:
        Run.objects.filter(id=run.id).update(
            state="assembling", assembly_lease=now + timedelta(minutes=2)
        )
