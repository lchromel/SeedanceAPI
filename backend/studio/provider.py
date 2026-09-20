"""Direct BytePlus adapter. Never imports the legacy HTTP app or desktop secrets."""

import ipaddress
import socket
import ssl
from urllib.parse import quote, urlparse

import requests
import urllib3
from django.conf import settings

from . import storage
from .models import Asset


class Rejected(Exception):
    pass


def payload(chunk):
    snapshot = chunk.run.snapshot
    labels = (
        ["the character"]
        + ["clothing to wear"] * len(snapshot.get("clothing", []))
        + (["the location"] if snapshot.get("location") else [])
    )
    references = " ".join(f"Reference image {i + 1} is {label}." for i, label in enumerate(labels))
    content = [{"type": "text", "text": references + "\n" + chunk.prompt}]
    ids = [
        snapshot.get("character"),
        *snapshot.get("clothing", []),
        snapshot.get("location"),
    ]
    for asset_id in filter(None, ids):
        asset = Asset.objects.get(id=asset_id, owner_id=chunk.run.project.owner_id)
        if asset.provider_id and asset.provider_status != "Active":
            raise Rejected("This character is no longer available in BytePlus Assets.")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"asset://{asset.provider_id}"
                    if asset.provider_id
                    else storage.url(asset.key, provider=True)
                },
                "role": "reference_image",
            }
        )
    if chunk.motion_asset_id:
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": storage.url(chunk.motion_asset.key, provider=True)},
                "role": "reference_video",
            }
        )
    return {
        "model": snapshot["model"],
        "content": content,
        "ratio": "9:16",
        "duration": chunk.duration,
        "resolution": "720p",
        "generate_audio": True,
    }


def submit(data):
    response = requests.post(
        settings.ARK_BASE_URL + "/contents/generations/tasks",
        json=data,
        headers={"Authorization": f"Bearer {settings.ARK_API_KEY}"},
        timeout=(10, 60),
        allow_redirects=False,
    )
    if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
        raise Rejected("The provider rejected this clip. Check the references and model settings.")
    response.raise_for_status()
    task_id = response.json().get("id")
    if not task_id:
        raise RuntimeError("Submission result is unknown")
    return str(task_id)


def status(task_id):
    response = requests.get(
        settings.ARK_BASE_URL + "/contents/generations/tasks/" + quote(task_id, safe=""),
        headers={"Authorization": f"Bearer {settings.ARK_API_KEY}"},
        timeout=(10, 30),
        allow_redirects=False,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("status", ""), (data.get("content") or {}).get("video_url")


def download(url, target, *, allowed_hosts=None, max_bytes=512 * 1024 * 1024):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Invalid output URL")
    if not any(
        host == h or host.endswith("." + h)
        for h in (settings.OUTPUT_HOSTS if allowed_hosts is None else allowed_hosts)
    ):
        raise ValueError("Output host is not allowed")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Output host must be public")
    # Pin the already-validated public IP to prevent DNS rebinding. TLS still
    # verifies the provider hostname and uses that hostname for SNI.
    address = addresses[0][4][0]
    pool = urllib3.HTTPSConnectionPool(
        address,
        port=443,
        server_hostname=host,
        assert_hostname=host,
        ssl_context=ssl.create_default_context(),
    )
    target_path = parsed.path or "/"
    if parsed.query:
        target_path += "?" + parsed.query
    response = None
    try:
        response = pool.request(
            "GET",
            target_path,
            headers={"Host": host},
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=urllib3.Timeout(connect=10, read=60),
        )
        if response.status != 200:
            raise ValueError("Output is unavailable")
        count = 0
        with open(target, "wb") as out:
            for block in response.stream(1024 * 1024):
                count += len(block)
                if count > max_bytes:
                    raise ValueError("Output is too large")
                out.write(block)
    finally:
        if response:
            response.close()
        pool.close()
