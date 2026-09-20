"""One reference order shared by prompt preview, enhancement and generation."""

import re

from rest_framework.exceptions import ValidationError

from .models import Asset

CATEGORIES = {
    "clothing": {
        "boots": "Boots",
        "shoes": "Shoes",
        "vest": "Vest",
        "top": "Top",
        "outerwear": "Outerwear",
        "trousers": "Trousers",
        "skirt": "Skirt",
        "dress": "Dress",
        "accessory": "Accessory",
        "other": "Other clothing",
    },
    "location": {
        "interior": "Interior",
        "exterior": "Exterior",
        "studio": "Studio",
        "urban": "Urban space",
        "nature": "Nature",
        "other": "Other location",
    },
}


def ordered_assets(user, config):
    selections = [(config.get("character"), "character"), (config.get("location"), "location")]
    selections += [(pk, "clothing") for pk in config.get("clothing", [])]
    result = []
    for pk, kind in selections:
        if pk:
            asset = Asset.objects.filter(pk=pk, owner=user, kind=kind).first()
            if not asset:
                raise ValidationError("A selected reference is unavailable.")
            result.append(asset)
    return result


def instructions(assets):
    lines = []
    for index, asset in enumerate(assets, 1):
        if asset.kind == "character":
            text = (
                "use for the person's identity, facial features, hairstyle, body proportions, "
                "skin tone, and base clothing. Preserve visible natural skin details, including "
                "uneven tanning where present. Clothing references override only their assigned "
                "garments; keep the person's identity unchanged."
            )
        elif asset.kind == "location":
            text = (
                "use for the location, architecture, floor surface, materials, colors, and lighting. "
                "Place the person naturally within this environment at a believable scale. "
                "Use the environment only, not incidental people."
            )
        else:
            category = CATEGORIES["clothing"].get(asset.category, "selected garment").lower()
            details = (
                "shape, shaft height, sole profile, material, color, and visible design details"
                if asset.category == "boots"
                else "shape, sole profile, material, color, and visible design details"
                if asset.category == "shoes"
                else "cut, length, color, material, fit, closures, pockets, and visible design details"
            )
            text = (
                f"use exclusively for the {category}, preserving its {details}. "
                "Transfer only this item to the selected person, not the donor's identity, "
                "body, other garments, or background. Apply explicit changes requested in the scene."
            )
        if asset.description:
            text += " Visible reference details: " + asset.description.strip()
        lines.append(f"@Image{index}: {text}")
    return "\n\n".join(lines)


def require_analysis(assets):
    if any(a.kind in CATEGORIES and a.analysis_status != "ready" for a in assets):
        raise ValidationError(
            "Analyze the selected clothing and location references in the library first."
        )


def validate_tags(text, assets):
    for match in re.finditer(r"@(?:image|img|video|audio)(\d+)\b", text, re.I):
        number = int(match.group(1))
        if not 1 <= number <= len(assets) or match.group(0) != f"@Image{number}":
            raise ValidationError(
                "The scene contains a reference tag that does not match the selected images."
            )
