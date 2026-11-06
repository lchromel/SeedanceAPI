"""One signed location identity shared by the individual prompts in a series."""

import hashlib
import time

from django.conf import settings
from django.core import signing
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from . import enhancer, image_retention, image_vision
from .models import Asset
from .image_reference_text import rewrite_observation_references

SALT = "studio.shared-location"
VERSION = 1


class ProfileRequest(serializers.Serializer):
    asset = serializers.UUIDField()


class SharedProfile(serializers.Serializer):
    asset = serializers.UUIDField(required=False)
    description = serializers.CharField(max_length=2000, required=False)
    proof = serializers.CharField(max_length=1000, required=False)

    def validate_asset(self, value):
        return str(value)


def validate(user, location, profile):
    if not profile:
        return
    try:
        payload = signing.loads(profile["proof"], salt=SALT, max_age=86400)
        valid = (
            payload["owner"] == user.pk
            and payload["asset"] == location == profile["asset"]
            and payload["version"] == VERSION
            and payload["model"] == settings.REFERENCE_VISION_MODEL
            and payload["description"]
            == hashlib.sha256(profile["description"].encode()).hexdigest()
        )
    except (signing.BadSignature, TypeError, KeyError, ValueError):
        valid = False
    if not valid:
        raise ValidationError("Prepare the shared location description again for this location.")


def create(user, asset_id):
    asset = image_retention.available_assets(
        Asset.objects.filter(owner=user, pk=asset_id, kind="image", mime="image/jpeg")
    ).first()
    if not asset:
        raise ValidationError("The location image is unavailable. Upload it again.")
    focus = (
        "Create the fixed visual identity of this LOCATION, to be reused unchanged across a photo series. "
        "Describe only observed facts in English, under 2000 characters: room/environment type, architectural "
        "finishes, exact observed color palette, main furniture design and materials, upholstery or bedding, "
        "distinctive fixtures and a few recognizable details. Separate facts from uncertainty. Prioritize "
        "stable defining features over a long inventory. For a hotel room, identify the same bed design, "
        "headboard material and shape, linen colors, wall finish and lights. Describe what the location "
        "looks like independently of this photograph's camera or any future subject. Preserve consistent "
        "design and materials across future crops. Leave unseen room areas and camera measurements unspecified."
    )
    observations = image_vision.analyze(
        [asset], time.monotonic() + 100, reference_focus={1: focus}, detailed_references=[1]
    )
    description = rewrite_observation_references(
        observations[0]["description"].strip(), {}, unknown="this location"
    )
    if len(description) > 2000:
        raise enhancer.EnhancementError(
            "The location description is too long. Try preparing it again."
        )
    profile = {"asset": str(asset.pk), "description": description}
    profile["proof"] = signing.dumps(
        {
            "owner": user.pk,
            "asset": str(asset.pk),
            "version": VERSION,
            "model": settings.REFERENCE_VISION_MODEL,
            "description": hashlib.sha256(description.encode()).hexdigest(),
        },
        salt=SALT,
    )
    return profile
