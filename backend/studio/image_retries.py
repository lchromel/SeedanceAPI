"""Explicit new image attempts, with fresh inputs when temporary originals are gone."""

import copy

from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from . import images, replacements
from .models import ImageGeneration, Wallet


class Regeneration(serializers.Serializer):
    requestKey = serializers.UUIDField()
    references = serializers.ListField(child=serializers.UUIDField(), max_length=10, required=False)


@transaction.atomic
def regenerate(user, source, data):
    Wallet.objects.get_or_create(user=user)
    Wallet.objects.select_for_update().get(user=user)
    existing = ImageGeneration.objects.filter(owner=user, request_key=data["requestKey"]).first()
    if existing:
        return existing
    source.refresh_from_db()
    if source.state not in ("ready", "error", "expired"):
        raise ValidationError(
            "Wait for this generation to finish. A submission under review cannot be resubmitted."
        )
    previous_refs = source.snapshot["references"]
    refs = [str(pk) for pk in data.get("references", previous_refs)]
    if len(refs) != len(previous_refs) or len(set(refs)) != len(refs):
        raise ValidationError("Reattach each original reference in its original order.")
    replacements.validate_assets(user, refs)
    job = images.generate(
        user,
        {
            "requestKey": data["requestKey"],
            "prompt": source.snapshot["prompt"],
            "references": refs,
            "ratio": source.snapshot["ratio"],
            "resolution": source.snapshot["resolution"],
        },
    )
    mapping = dict(zip(previous_refs, refs))
    for field in (
        "mode",
        "batch_id",
        "original_name",
        "original_index",
        "general_wishes",
        "individual_wishes",
        "keep_tattoos",
        "identity_lock",
        "location_description_only",
        "location_geometry_version",
        "location_description",
        "location_profile",
        "structured_prompt",
    ):
        if field in source.snapshot:
            job.snapshot[field] = copy.deepcopy(source.snapshot[field])
    if "original_id" in source.snapshot:
        job.snapshot["original_id"] = mapping.get(
            source.snapshot["original_id"], source.snapshot["original_id"]
        )
    if "replacement_roles" in source.snapshot:
        job.snapshot["replacement_roles"] = {
            role: mapping.get(pk, pk) for role, pk in source.snapshot["replacement_roles"].items()
        }
    if "extra_references" in source.snapshot:
        job.snapshot["extra_references"] = [
            {**item, "asset": mapping.get(item["asset"], item["asset"])}
            for item in source.snapshot["extra_references"]
        ]
    job.snapshot["regenerated_from"] = str(source.pk)
    job.save(update_fields=["snapshot"])
    return job
