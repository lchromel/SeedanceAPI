"""One independent replacement prompt and durable generation job per original."""

import math
import tempfile
import uuid
from pathlib import Path

from django.db import transaction
from PIL import Image
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from . import image_retention, images, storage
from .models import Asset, ImageGeneration, Wallet

ROLES = ("model", "clothing", "makeup", "location")
ASPECTS = {
    "model": "the primary person's identity and overall appearance",
    "clothing": "the clothing and accessories",
    "makeup": "the makeup",
    "location": "the location and background",
}
SYSTEM = (Path(__file__).with_name("prompts") / "seedream_replacement.md").read_text()


class ReplacementReferences(serializers.Serializer):
    model = serializers.UUIDField(allow_null=True, default=None)
    clothing = serializers.UUIDField(allow_null=True, default=None)
    makeup = serializers.UUIDField(allow_null=True, default=None)
    location = serializers.UUIDField(allow_null=True, default=None)

    def validate(self, data):
        return {key: str(value) if value else None for key, value in data.items()}


class ReplacementCommon(serializers.Serializer):
    replacements = ReplacementReferences(default=dict)
    instructions = serializers.CharField(max_length=2000, allow_blank=True, default="")
    ratio = serializers.ChoiceField(choices=["original", *images.RATIOS], default="original")
    resolution = serializers.ChoiceField(choices=list(images.RESOLUTIONS), default="2K")


def reference_ids(original, replacements):
    return list(
        dict.fromkeys([original, *filter(None, (replacements.get(role) for role in ROLES))])
    )


def validate_assets(user, ids):
    ids = set(ids)
    if image_retention.available_assets(
        Asset.objects.filter(
            owner=user,
            kind="image",
            mime="image/jpeg",
            pk__in=ids,
        )
    ).count() != len(ids):
        raise ValidationError(
            "An original or replacement reference is unavailable. Upload it again."
        )


class ReplacementPrompt(ReplacementCommon):
    original = serializers.UUIDField()
    prompt = serializers.CharField(max_length=6000, allow_blank=True, default="")
    notes = serializers.CharField(max_length=2000, allow_blank=True, default="")

    def validate(self, data):
        data["original"] = str(data["original"])
        validate_assets(self.context["user"], reference_ids(data["original"], data["replacements"]))
        return data


class OriginalSelection(serializers.Serializer):
    asset = serializers.UUIDField()
    prompt = serializers.CharField(max_length=6000)
    notes = serializers.CharField(max_length=2000, allow_blank=True, default="")

    def validate_asset(self, value):
        return str(value)


class ReplacementBatch(ReplacementCommon):
    requestKey = serializers.UUIDField()
    originals = OriginalSelection(many=True, min_length=1, max_length=50)

    def validate(self, data):
        originals = [item["asset"] for item in data["originals"]]
        if len(originals) != len(set(originals)):
            raise ValidationError("Each original must appear only once in a series.")
        validate_assets(
            self.context["user"], [*originals, *filter(None, data["replacements"].values())]
        )
        return data


def plan_for(original, replacements):
    ids = reference_ids(original, replacements)
    return {
        role: {
            "aspect": ASPECTS[role],
            "source": f"Image {ids.index(replacements[role]) + 1}"
            if replacements.get(role)
            else "original",
        }
        for role in ROLES
    }


def original_ratio(asset):
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "original"
            storage.fetch(asset.key, path)
            with Image.open(path) as image:
                width, height = image.size
        divisor = math.gcd(width, height)
        return f"{width // divisor}:{height // divisor}"
    except FileNotFoundError:
        raise ValidationError("An original expired. Upload it again.") from None


def base_prompt(plan):
    parts = [
        "Edit Image 1, the original photograph, preserving its pose, expression, camera viewpoint and composition."
    ]
    for item in plan.values():
        if item["source"] == "original":
            parts.append(f"Preserve {item['aspect']} from Image 1.")
        else:
            parts.append(f"Use {item['source']} for {item['aspect']}.")
    parts.append(
        "Integrate changes naturally with coherent perspective, lighting and contact shadows."
    )
    return " ".join(parts)


def enhance(user, data):
    original = Asset.objects.get(owner=user, pk=data["original"])
    plan = plan_for(data["original"], data["replacements"])
    ratio = original_ratio(original) if data["ratio"] == "original" else data["ratio"]
    result = images.enhance(
        user,
        {
            "prompt": data["prompt"] or base_prompt(plan),
            "instructions": data["instructions"],
            "references": reference_ids(data["original"], data["replacements"]),
            "ratio": ratio,
            "resolution": data["resolution"],
        },
        system=SYSTEM,
        extra_context={
            "replacementPlan": plan,
            "individualWishes": data["notes"],
        },
    )
    return {**result, "original": data["original"], "ratio": ratio}


def existing_batch(user, request_key):
    return list(
        ImageGeneration.objects.filter(
            owner=user,
            snapshot__batch_id=str(request_key),
            snapshot__mode="replacement",
        ).order_by("snapshot__original_index", "id")
    )


@transaction.atomic
def generate(user, data):
    Wallet.objects.get_or_create(user=user)
    Wallet.objects.select_for_update().get(user=user)
    previous = existing_batch(user, data["requestKey"])
    if previous:
        return previous
    assets = {
        str(a.pk): a
        for a in Asset.objects.filter(
            owner=user, pk__in=[item["asset"] for item in data["originals"]]
        )
    }
    if len(assets) != len(data["originals"]):
        raise ValidationError("An original is unavailable. Upload it again.")
    jobs = []
    copies = []
    try:
        for index, item in enumerate(data["originals"]):
            original = assets[item["asset"]]
            ratio = original_ratio(original) if data["ratio"] == "original" else data["ratio"]
            job = images.generate(
                user,
                {
                    "prompt": item["prompt"],
                    "ratio": ratio,
                    "resolution": data["resolution"],
                    "references": reference_ids(item["asset"], data["replacements"]),
                    "requestKey": uuid.uuid5(data["requestKey"], item["asset"]),
                },
            )
            copies.extend(job.snapshot["reference_keys"])
            job.snapshot.update(
                {
                    "mode": "replacement",
                    "batch_id": str(data["requestKey"]),
                    "original_id": item["asset"],
                    "original_name": original.name,
                    "original_index": index,
                    "replacement_roles": data["replacements"],
                    "general_wishes": data["instructions"],
                    "individual_wishes": item["notes"],
                }
            )
            job.save(update_fields=["snapshot"])
            jobs.append(job)
    except Exception:
        try:
            storage.delete_temporary(copies)
        except Exception:
            pass  # Copies also have a TTL if Redis is temporarily unreachable.
        raise
    return jobs
