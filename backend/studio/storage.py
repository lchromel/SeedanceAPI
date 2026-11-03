import boto3
import base64
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from django.conf import settings
from redis import Redis


def is_temporary(key):
    return bool(
        re.fullmatch(
            r"\d+/temporary-images/[0-9a-f-]+/(?:reference|preview|output|input-\d+)\.jpg",
            key or "",
        )
    )


@lru_cache(maxsize=4)
def _temporary_client(url):
    return Redis.from_url(url, socket_connect_timeout=5, socket_timeout=10)


def temporary_client():
    if not settings.IMAGE_TEMP_REDIS_URL:
        raise RuntimeError("Temporary image storage is not configured")
    return _temporary_client(settings.IMAGE_TEMP_REDIS_URL)


def temporary_bytes(key):
    if not is_temporary(key):
        raise ValueError("Invalid temporary image key")
    data = temporary_client().get("studio:temporary-image:" + key)
    if data is None:
        raise FileNotFoundError("Temporary image expired")
    return data


def delete_temporary(keys):
    keys = ["studio:temporary-image:" + key for key in keys if is_temporary(key)]
    if keys:
        temporary_client().delete(*keys)


def client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
    )


def put(key, stream, mime, *, ttl=None):
    if is_temporary(key):
        data = stream.read(30 * 1024 * 1024 + 1)
        if len(data) > 30 * 1024 * 1024:
            raise ValueError("Temporary image is too large")
        temporary_client().set(
            "studio:temporary-image:" + key,
            data,
            ex=ttl if ttl is not None else settings.IMAGE_REFERENCE_TTL_SECONDS,
        )
    elif settings.S3_BUCKET:
        client().upload_fileobj(stream, settings.S3_BUCKET, key, ExtraArgs={"ContentType": mime})
    elif settings.DEBUG:
        path = settings.MEDIA_ROOT / key
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as out:
            import shutil

            shutil.copyfileobj(stream, out)
    else:
        raise RuntimeError("Private storage is not configured")


def fetch(key, target):
    if is_temporary(key):
        Path(target).write_bytes(temporary_bytes(key))
    elif settings.S3_BUCKET:
        client().download_file(settings.S3_BUCKET, key, str(target))
    else:
        import shutil

        shutil.copyfile(settings.MEDIA_ROOT / key, target)


def url(key, provider=False, download=False):
    if not key:
        return None
    if provider and is_temporary(key):
        return "data:image/jpeg;base64," + base64.b64encode(temporary_bytes(key)).decode()
    if settings.S3_BUCKET and provider:
        params = {"Bucket": settings.S3_BUCKET, "Key": key}
        return client().generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=3600,
        )
    if provider:
        raise RuntimeError("External generation requires S3 storage")
    return "/api/media/" + quote(key, safe="/") + ("?download=1" if download else "")
