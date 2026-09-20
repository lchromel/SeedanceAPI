"""Private BytePlus Assets catalog. Credentials and provider URLs stay server-side."""

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction

from .models import Asset


class CatalogError(Exception):
    pass


def call(action, payload):
    access = settings.BYTEPLUS_ACCESS_KEY_ID
    secret = settings.BYTEPLUS_SECRET_ACCESS_KEY
    if not access or not secret:
        raise CatalogError("BytePlus Assets is not configured. Contact your administrator.")
    host = "ark.ap-southeast-1.byteplusapi.com"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(body).hexdigest()
    query = urlencode({"Action": action, "Version": "2024-01-01"})
    signed = "content-type;host;x-content-sha256;x-date"
    headers = (
        f"content-type:application/json\nhost:{host}\nx-content-sha256:{digest}\nx-date:{date}\n"
    )
    canonical = "\n".join(["POST", "/", query, headers, signed, digest])
    scope = f"{date[:8]}/ap-southeast-1/ark/request"
    message = "\n".join(
        ["HMAC-SHA256", date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = secret.encode()
    for value in (date[:8], "ap-southeast-1", "ark", "request"):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    try:
        response = requests.post(
            f"https://{host}/?{query}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Host": host,
                "X-Date": date,
                "X-Content-Sha256": digest,
                "Authorization": f"HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}",
            },
            timeout=(5, 20),
            allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("ResponseMetadata", {}).get("Error"):
            raise ValueError()
        result = data.get("Result")
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (requests.RequestException, ValueError, TypeError):
        # Never return signed URLs, account details or provider error bodies.
        raise CatalogError("BytePlus Assets is unavailable. Try refreshing the library.") from None


def catalog():
    items = {}
    for group in ("AIGC", "LivenessFace"):
        for page in range(1, 101):
            result = call(
                "ListAssets",
                {
                    "ProjectName": settings.BYTEPLUS_ASSET_PROJECT,
                    "Filter": {"GroupType": group},
                    "PageNumber": page,
                    "PageSize": 100,
                    "SortBy": "CreateTime",
                    "SortOrder": "Desc",
                },
            )
            batch = result.get("Items", [])
            if not isinstance(batch, list):
                raise CatalogError("BytePlus returned an invalid asset list.")
            for item in batch:
                asset_id = str(item.get("Id") or item.get("AssetId") or "")
                if item.get("AssetType") == "Image" and re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,200}", asset_id
                ):
                    items[asset_id] = item
            if page * 100 >= int(result.get("TotalCount", 0)) and len(batch) < 100:
                break
        else:
            raise CatalogError("The BytePlus library is too large to sync in one request.")
    return items


def sync(user):
    # Fetch all pages first: an upstream failure must not erase the cached catalog.
    items = catalog()
    with transaction.atomic():
        for asset_id, item in items.items():
            Asset.objects.update_or_create(
                owner=user,
                provider_id=asset_id,
                provider_project=settings.BYTEPLUS_ASSET_PROJECT,
                defaults={
                    "kind": "character",
                    "name": str(item.get("Name") or asset_id)[:120],
                    "provider_status": str(item.get("Status") or "Unknown")[:32],
                    "key": "",
                    "mime": "image/jpeg",
                },
            )
        query = Asset.objects.filter(
            owner=user, provider_project=settings.BYTEPLUS_ASSET_PROJECT
        ).exclude(provider_id="")
        query.exclude(provider_id__in=items).update(provider_status="Unavailable")
    return query.exclude(provider_status="Unavailable").order_by("-created")


def detail(asset):
    return call("GetAsset", {"Id": asset.provider_id, "ProjectName": asset.provider_project})
