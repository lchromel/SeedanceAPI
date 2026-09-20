import uuid

from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ValidationError

from . import references
from .billing import post
from .models import Asset, Chunk, Project, Run


def split_duration(seconds, maximum):
    if maximum < 4:
        raise ValidationError("Provider duration limit is not configured.")
    sizes = []
    while seconds:
        size = min(seconds, maximum, 30)
        if 0 < seconds - size < 4:
            size -= 4 - (seconds - size)
        if size < 4:
            raise ValidationError("A provider clip must be at least 4 seconds.")
        sizes.append(size)
        seconds -= size
    return sizes


def dialogue_slice(text, start, duration, total):
    words = text.split()
    return " ".join(words[len(words) * start // total : len(words) * (start + duration) // total])


@transaction.atomic
def generate(user, project_id, part, request_key):
    project = Project.objects.select_for_update(of=("self",)).get(id=project_id, owner=user)
    try:
        request_key = uuid.UUID(str(request_key))
    except (ValueError, TypeError):
        raise ValidationError("A generation request ID is required.")
    existing = project.runs.filter(request_key=request_key).first()
    if existing:
        return existing
    if part not in ("freeform", "motion"):
        raise ValidationError("Unknown part.")
    if project.runs.filter(part=part, state__in=["generating", "assembling", "review"]).exists():
        raise ValidationError("This part already has an active generation.")
    if settings.CREDITS_PER_SECOND is None or settings.CREDITS_PER_SECOND < 0:
        raise ValidationError("An administrator must configure credit pricing first.")
    if settings.GENERATION_PROVIDER != "mock" and (
        not settings.ARK_API_KEY
        or not settings.ARK_MODEL
        or not settings.S3_BUCKET
        or not settings.OUTPUT_HOSTS
    ):
        raise ValidationError(
            "The generation provider and private storage must be configured first."
        )
    config = project.config
    if not config.get("character"):
        raise ValidationError("Choose a character first.")
    character = Asset.objects.filter(id=config["character"], owner=user).first()
    if not character or (character.provider_id and character.provider_status != "Active"):
        raise ValidationError("This character is unavailable. Refresh the character library.")
    if part == "freeform" and not config.get("action"):
        raise ValidationError("Describe the action first.")
    if part == "motion" and not config.get("motions"):
        raise ValidationError("Choose at least one motion preset.")
    assets = references.ordered_assets(user, config)
    references.require_analysis(assets)
    references.validate_tags(config.get("action", ""), assets)
    snapshot = {
        **config,
        "reference_ids": [str(a.pk) for a in assets],
        "reference_instructions": references.instructions(assets),
        "provider": settings.GENERATION_PROVIDER,
        "model": settings.ARK_MODEL,
        "revision": project.revision,
    }
    run = Run.objects.create(project=project, part=part, snapshot=snapshot, request_key=request_key)
    pieces = (
        [(config["duration"], None)]
        if part == "freeform"
        else [(m["duration"], m["asset"]) for m in config["motions"]]
    )
    start = 0 if part == "freeform" else config["duration"]
    index = 0
    for seconds, motion in pieces:
        for duration in split_duration(seconds, settings.PROVIDER_MAX_SECONDS):
            prompt = f"Vertical 9:16 video. Keep the same character, clothing and location across the sequence. Scene time {start}–{start + duration} seconds. "
            prompt += config.get("action", "")
            if part == "motion":
                prompt += " Follow the reference video motion."
            if config.get("speech") and part == "freeform":
                lines = dialogue_slice(config["speech"], start, duration, config["duration"])
                prompt += (
                    "\nSpeak only this assigned dialogue, without repeating earlier lines: "
                    + (lines or "[No dialogue in this clip]")
                )
            chunk = Chunk.objects.create(
                run=run,
                position=index,
                start=start,
                duration=duration,
                prompt=prompt,
                motion_asset_id=motion,
                cost=duration * settings.CREDITS_PER_SECOND,
            )
            post(user.id, "reserve", chunk.cost, f"{chunk.id}:1:reserve")
            start += duration
            index += 1
    # Database is the durable outbox; beat discovers these records even if Redis is down.
    return run


@transaction.atomic
def retry(user, chunk_id):
    chunk = (
        Chunk.objects.select_for_update(of=("self",))
        .select_related("run__project")
        .get(id=chunk_id, run__project__owner=user)
    )
    if chunk.state != "error":
        raise ValidationError("Only a confirmed failed clip can be retried.")
    if chunk.run.project.runs.filter(part=chunk.run.part, created__gt=chunk.run.created).exists():
        raise ValidationError("A newer version of this part exists.")
    chunk.attempt += 1
    post(user.id, "reserve", chunk.cost, f"{chunk.id}:{chunk.attempt}:reserve")
    chunk.state, chunk.error, chunk.provider_id = "queued", "", ""
    chunk.lease_until = chunk.next_poll = chunk.started = None
    chunk.save()
    chunk.run.state, chunk.run.output_key = "generating", ""
    chunk.run.save(update_fields=["state", "output_key"])
    return chunk
