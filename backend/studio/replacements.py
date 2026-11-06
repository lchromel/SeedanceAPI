"""One independent replacement prompt and durable generation job per original."""

import math
import hashlib
import json
import tempfile
import uuid
from pathlib import Path

from django.db import transaction
from django.core import signing
from PIL import Image
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from . import image_retention, images, storage
from .models import Asset, ImageGeneration, Wallet

ROLES = ("model", "clothing", "makeup", "hairstyle", "location")
ASPECTS = {
    "model": "the primary person's identity and overall appearance",
    "clothing": "the clothing and accessories",
    "makeup": "the makeup",
    "hairstyle": "the hairstyle",
    "location": "the location and background",
}
SYSTEM = (Path(__file__).with_name("prompts") / "seedream_replacement.md").read_text()
TATTOO_INSTRUCTIONS = {
    False: "Remove all tattoos from visible skin, including tattoos on the original subject or replacement model; render natural, unmarked skin with realistic texture.",
    True: "Preserve visible tattoos from the new model reference when supplied, otherwise from Image 1, following their observed placement and appearance on visible skin; keep covered or unobserved areas unspecified.",
}
WORKFLOWS = {
    "replacement": ("model", "clothing", "makeup", "hairstyle", "location"),
    "makeup": ("makeup", "hairstyle"),
    "wardrobe": ("clothing",),
}
IDENTITY_MARKER = "\n\nFixed identity details:\n"
WORKFLOW_MARKER = "\n\nFocused edit scope:\n"
LOCATION_GEOMETRY_MARKER = "\n\nLocation geometry:\n"
LOCATION_GEOMETRY_VERSION = 2
LOCATION_GEOMETRY_INSTRUCTION = (
    "Use Image 1 as the camera, composition and body-contact anchor. Keep the person's pose and the "
    "perspective, position, scale and visible count of furniture supporting their body. Restyle that "
    "same furniture using the described location's materials and design. Each matched support is one "
    "object represented once, with its surface, textiles and structural parts in one coherent "
    "perspective. Integrate compatible "
    "room details around these anchors, preserving contact, occlusion and the original crop."
)
WORKFLOW_INSTRUCTIONS = {
    "makeup": "Edit only the makeup and selected hairstyle on the original portrait. Preserve the original person's facial identity, bone structure, feature proportions, age, expression, gaze, base skin tone, pose, clothing, background and framing, subject to the explicit identity and tattoo settings. Donor references supply cosmetics and hair rather than a different face.",
    "wardrobe": "Dress the person from the original photograph or body sheet in the referenced garments. Preserve facial identity, body proportions, pose, camera, background and the arrangement of existing views, subject to the explicit identity and tattoo settings. Apply the same outfit consistently to every existing view, matching observed fabric, cut and colors with natural drape and fit.",
}


class IdentityDetails(serializers.Serializer):
    eyeColor = serializers.ChoiceField(
        choices=["", "brown", "blue", "green", "hazel", "gray", "amber"],
        default="",
        allow_blank=True,
    )
    details = serializers.CharField(max_length=600, allow_blank=True, default="")


class ExtraReference(serializers.Serializer):
    asset = serializers.UUIDField()
    label = serializers.CharField(max_length=160)

    def validate_asset(self, value):
        return str(value)


class ReplacementReferences(serializers.Serializer):
    model = serializers.UUIDField(allow_null=True, default=None)
    clothing = serializers.UUIDField(allow_null=True, default=None)
    makeup = serializers.UUIDField(allow_null=True, default=None)
    hairstyle = serializers.UUIDField(allow_null=True, default=None)
    location = serializers.UUIDField(allow_null=True, default=None)

    def validate(self, data):
        return {key: str(value) if value else None for key, value in data.items()}


class ReplacementCommon(serializers.Serializer):
    replacements = ReplacementReferences(default=dict)
    instructions = serializers.CharField(max_length=2000, allow_blank=True, default="")
    ratio = serializers.ChoiceField(choices=["original", *images.RATIOS], default="original")
    resolution = serializers.ChoiceField(choices=list(images.RESOLUTIONS), default="2K")
    keepTattoos = serializers.BooleanField(default=False)
    workflow = serializers.ChoiceField(choices=list(WORKFLOWS), default="replacement")
    extras = ExtraReference(many=True, max_length=9, default=list)
    identity = IdentityDetails(default=dict)
    locationDescriptionOnly = serializers.BooleanField(default=False)

    def validate(self, data):
        allowed = WORKFLOWS[data["workflow"]]
        if any(value for role, value in data["replacements"].items() if role not in allowed):
            raise ValidationError("A reference role does not belong to the selected mode.")
        if data["workflow"] == "makeup" and not any(data["replacements"].values()):
            raise ValidationError("Add a makeup or hairstyle reference.")
        if data["workflow"] == "wardrobe" and not (
            data["replacements"].get("clothing") or data["extras"]
        ):
            raise ValidationError("Add at least one clothing reference.")
        if data["locationDescriptionOnly"]:
            location = data["replacements"].get("location")
            if data["workflow"] != "replacement" or not location:
                raise ValidationError("Add a location reference to use it only for description.")
            other_sources = [
                value for role, value in data["replacements"].items() if role != "location"
            ] + [item["asset"] for item in data["extras"]]
            if location in other_sources:
                raise ValidationError(
                    "Use a separate location image that is not assigned another reference role."
                )
        return data


def reference_ids(original, replacements, extras=()):
    return list(
        dict.fromkeys(
            [
                original,
                *filter(None, (replacements.get(role) for role in ROLES)),
                *(item["asset"] for item in extras),
            ]
        )
    )


def generation_ids(original, data):
    ids = reference_ids(original, data["replacements"], data["extras"])
    if data["locationDescriptionOnly"]:
        location = data["replacements"]["location"]
        if location == original:
            raise ValidationError(
                "The original and description-only location must be different images."
            )
        ids.remove(location)
    return ids


def preparation_fingerprint(original, data, notes):
    context = {
        key: data[key]
        for key in (
            "replacements",
            "extras",
            "instructions",
            "ratio",
            "resolution",
            "keepTattoos",
            "identity",
        )
    }
    context.update(
        original=original, notes=notes, locationGeometryVersion=LOCATION_GEOMETRY_VERSION
    )
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def location_proof(original, data, notes, description):
    return signing.dumps(
        {
            "context": preparation_fingerprint(original, data, notes),
            "description": hashlib.sha256(description.encode()).hexdigest(),
        },
        salt="studio.location-description",
    )


def validate_location_preparation(original, data, item):
    if not data["locationDescriptionOnly"]:
        return
    try:
        proof = signing.loads(
            item["locationPreparation"], salt="studio.location-description", max_age=86400
        )
        valid = (
            proof["context"] == preparation_fingerprint(original, data, item["notes"])
            and proof["description"]
            == hashlib.sha256(item["locationDescription"].encode()).hexdigest()
            and bool(item["locationDescription"].strip())
        )
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValidationError(
            "Prepare this photo's prompt again to describe the location with the current settings."
        )
    expected = set(range(1, len(generation_ids(original, data)) + 1))
    if images.reference_numbers(item["prompt"]) != expected:
        raise ValidationError(
            "Update the prompt to use only the attached generation image labels; the location is text only."
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
        data = super().validate(data)
        data["original"] = str(data["original"])
        ids = reference_ids(data["original"], data["replacements"], data["extras"])
        validate_reference_limit(ids)
        generation_ids(data["original"], data)
        validate_assets(self.context["user"], ids)
        return data


class OriginalSelection(serializers.Serializer):
    asset = serializers.UUIDField()
    prompt = serializers.CharField(max_length=6000)
    notes = serializers.CharField(max_length=2000, allow_blank=True, default="")
    locationDescription = serializers.CharField(max_length=2400, allow_blank=True, default="")
    locationPreparation = serializers.CharField(max_length=1000, allow_blank=True, default="")

    def validate_asset(self, value):
        return str(value)


class ReplacementBatch(ReplacementCommon):
    requestKey = serializers.UUIDField()
    originals = OriginalSelection(many=True, min_length=1, max_length=50)

    def validate(self, data):
        data = super().validate(data)
        originals = [item["asset"] for item in data["originals"]]
        if data["workflow"] != "replacement" and len(originals) != 1:
            raise ValidationError("This mode edits one portrait or body sheet at a time.")
        if len(originals) != len(set(originals)):
            raise ValidationError("Each original must appear only once in a series.")
        ids = []
        for original in originals:
            selected = reference_ids(original, data["replacements"], data["extras"])
            validate_reference_limit(selected)
            ids.extend(selected)
        for item in data["originals"]:
            validate_location_preparation(item["asset"], data, item)
        validate_assets(self.context["user"], ids)
        return data


def validate_reference_limit(ids):
    if len(ids) > 10:
        raise ValidationError("Use up to 10 images per generation, including the original.")


def plan_for(original, replacements, extras=(), *, ids=None, description_only=False):
    ids = ids if ids is not None else reference_ids(original, replacements, extras)
    return {
        role: {
            "aspect": ASPECTS[role],
            "source": "text description"
            if role == "location" and description_only
            else f"Image {ids.index(replacements[role]) + 1}"
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


def reference_focus(plan, keep_tattoos, description_only=False):
    """Observe the source camera and donor environment before DeepSeek stages the edit."""
    location = plan["location"]["source"]
    focus = {}
    if location != "original":
        focus[1] = (
            "Original photograph: describe the shot size, crop, camera height and viewing direction, "
            "subject position and scale, pose, visible support/contact surfaces, foreground/background "
            "depth and light direction. Identify furniture supporting the body (bed, sofa, chair or other), "
            "its visible count, mattress/seat plane, edges, orientation, contact points and body occlusion. "
            "Distinguish observable geometry from uncertain estimates."
        )
        number = int(location.split()[1])
        focus[number] = (
            focus.get(number, "")
            + " "
            + (
                "Location reference: describe the visible environment itself: spatial layout, architecture, "
                "surfaces, materials, colors, distinctive objects, depth layers, perspective and light sources. "
                "These observed features will be restaged around the original photograph's subject and camera. "
                "Keep unseen areas unspecified; distinguish scene facts from inferred geometry."
            )
        ).strip()
        if description_only:
            focus[number] += (
                " This image will NOT be sent to image generation. Compare it directly with Image 1. "
                "Match its bed or other supporting furniture to the existing support beneath the person. "
                "Describe how to restyle that same object, retaining its count, original camera and contact "
                "geometry; then describe compatible surroundings. Give a detailed English transfer plan "
                "in 250–300 words, under 2400 characters: spatial layout, dimensions only when observable, "
                "architecture, surface textures and colors, distinct objects, depth, light direction and "
                "quality, shadows and reflections. Consider the original photo's camera and crop when "
                "prioritizing visible features. Mark uncertainty and keep hidden geometry unspecified."
            )
    if keep_tattoos:
        source = plan["model"]["source"]
        number = 1 if source == "original" else int(source.split()[1])
        focus[number] = (
            focus.get(number, "")
            + " "
            + (
                "Describe any visible tattoos on this person: observed design, color, size and anatomical "
                "placement. Distinguish tattoos from makeup, clothing and natural skin features. "
                "Keep covered or unobserved skin unspecified."
            )
        ).strip()
    for role, task in {
        "model": "Describe the identity source's visible facial structure, eye shape and iris color, nose, lips, eyebrows and distinctive permanent features. Mark uncertain colors under lighting as uncertain.",
        "makeup": "Describe only the cosmetic reference's makeup placement, shades, texture and finish on eyes, brows, cheeks and lips. Separate cosmetic effects from face anatomy and iris color.",
        "hairstyle": "Describe the hair reference's parting, silhouette, length, texture, styling and color. Keep its person's face separate from hair attributes.",
        "clothing": "Describe the garments, accessories, cut, fabric, colors, closures and fit. For a body sheet, observe each existing view and the sheet layout.",
    }.items():
        source = plan[role]["source"]
        if source != "original" or role == "model":
            number = 1 if source == "original" else int(source.split()[1])
            focus[number] = f"{focus.get(number, '')} {task}".strip()
    return focus


def apply_tattoo_policy(prompt, keep_tattoos):
    # Replace our own previous suffix when improving a draft or toggling the setting.
    prompt = prompt.strip()
    while any(prompt.endswith(text) for text in TATTOO_INSTRUCTIONS.values()):
        for text in TATTOO_INSTRUCTIONS.values():
            if prompt.endswith(text):
                prompt = prompt[: -len(text)].rstrip()
    result = f"{prompt}\n\n{TATTOO_INSTRUCTIONS[keep_tattoos]}"
    if len(result) > 6000:
        raise ValidationError(
            "The prompt is too long. Shorten it to leave room for the tattoo setting."
        )
    return result


def apply_controls(prompt, data):
    # A prepared prompt may already contain the previous settings footer.
    for marker in (IDENTITY_MARKER, WORKFLOW_MARKER, LOCATION_GEOMETRY_MARKER):
        prompt = prompt.split(marker, 1)[0]
    prompt = apply_tattoo_policy(prompt, data["keepTattoos"])
    if data.get("locationDescriptionOnly"):
        prompt += LOCATION_GEOMETRY_MARKER + LOCATION_GEOMETRY_INSTRUCTION
    if data.get("workflow") in WORKFLOW_INSTRUCTIONS:
        prompt += WORKFLOW_MARKER + WORKFLOW_INSTRUCTIONS[data["workflow"]]
    identity = data.get("identity", {})
    details = []
    if identity.get("eyeColor"):
        details.append(
            f"Keep the person's iris color consistently {identity['eyeColor']} in every view; preserve this color under the scene lighting."
        )
    if identity.get("details"):
        details.append(identity["details"])
    if details:
        prompt += IDENTITY_MARKER + " ".join(details)
    if len(prompt) > 6000:
        raise ValidationError("Shorten the prompt to leave room for the fixed identity details.")
    return prompt


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
    ids = reference_ids(data["original"], data["replacements"], data["extras"])
    generation_refs = generation_ids(data["original"], data)
    analysis_plan = plan_for(data["original"], data["replacements"], data["extras"])
    plan = plan_for(
        data["original"],
        data["replacements"],
        data["extras"],
        ids=generation_refs,
        description_only=data["locationDescriptionOnly"],
    )
    ratio = original_ratio(original) if data["ratio"] == "original" else data["ratio"]
    result = images.enhance(
        user,
        {
            "prompt": data["prompt"] or base_prompt(plan),
            "instructions": data["instructions"],
            "references": ids,
            "ratio": ratio,
            "resolution": data["resolution"],
        },
        system=SYSTEM,
        extra_context={
            "replacementPlan": plan,
            "individualWishes": data["notes"],
            "tattooPolicy": TATTOO_INSTRUCTIONS[data["keepTattoos"]],
            "workflow": data["workflow"],
            "identityLock": data["identity"],
            "locationDescriptionOnly": data["locationDescriptionOnly"],
            "extraReferences": [
                {
                    "source": f"Image {generation_refs.index(item['asset']) + 1}",
                    "role": item["label"],
                }
                for item in data["extras"]
            ],
        },
        reference_focus=reference_focus(
            analysis_plan, data["keepTattoos"], data["locationDescriptionOnly"]
        ),
        description_only=[data["replacements"]["location"]]
        if data["locationDescriptionOnly"]
        else [],
    )
    prompt = apply_controls(result["prompt"], data)
    if data["locationDescriptionOnly"]:
        result["locationPreparation"] = location_proof(
            data["original"], data, data["notes"], result["locationDescription"]
        )
    return {**result, "prompt": prompt, "original": data["original"], "ratio": ratio}


def existing_batch(user, request_key):
    return list(
        ImageGeneration.objects.filter(
            owner=user,
            snapshot__batch_id=str(request_key),
            snapshot__mode__in=list(WORKFLOWS),
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
                    "prompt": apply_controls(item["prompt"], data),
                    "ratio": ratio,
                    "resolution": data["resolution"],
                    "references": generation_ids(item["asset"], data),
                    "requestKey": uuid.uuid5(data["requestKey"], item["asset"]),
                },
            )
            copies.extend(job.snapshot["reference_keys"])
            job.snapshot.update(
                {
                    "mode": data["workflow"],
                    "batch_id": str(data["requestKey"]),
                    "original_id": item["asset"],
                    "original_name": original.name,
                    "original_index": index,
                    "replacement_roles": data["replacements"],
                    "general_wishes": data["instructions"],
                    "individual_wishes": item["notes"],
                    "keep_tattoos": data["keepTattoos"],
                    "identity_lock": data["identity"],
                    "extra_references": data["extras"],
                    "location_description_only": data["locationDescriptionOnly"],
                    "location_geometry_version": LOCATION_GEOMETRY_VERSION
                    if data["locationDescriptionOnly"]
                    else None,
                    "location_description": item["locationDescription"]
                    if data["locationDescriptionOnly"]
                    else "",
                    "analysis_reference_ids": [data["replacements"]["location"]]
                    if data["locationDescriptionOnly"]
                    else [],
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
