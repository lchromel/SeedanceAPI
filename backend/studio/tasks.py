import tempfile
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import media, provider, storage
from .billing import post
from .models import Chunk, LoginBucket, Run


@shared_task
def dispatch():
    now = timezone.now()
    LoginBucket.objects.filter(window__lt=now - timedelta(days=1)).delete()
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
        post(
            chunk.run.project.owner_id,
            "settle" if state == "ready" else "release",
            chunk.cost,
            f"{chunk.id}:{chunk.attempt}:final",
        )
        chunk.state, chunk.output_key, chunk.error = state, key, message
        chunk.lease_until = None
        chunk.save()


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
                    error="Provider task needs review; credits remain reserved.",
                )
            Chunk.objects.filter(id=chunk.id).update(
                lease_until=None, next_poll=now + timedelta(seconds=15)
            )
    except Exception:
        if now - chunk.started > timedelta(hours=24):
            Chunk.objects.filter(id=chunk.id).update(
                state="review",
                error="The task needs administrator review; credits remain reserved.",
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
