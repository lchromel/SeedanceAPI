import boto3
from urllib.parse import quote
from django.conf import settings


def client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
    )


def put(key, stream, mime):
    if settings.S3_BUCKET:
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
    if settings.S3_BUCKET:
        client().download_file(settings.S3_BUCKET, key, str(target))
    else:
        import shutil

        shutil.copyfile(settings.MEDIA_ROOT / key, target)


def url(key, provider=False, download=False):
    if not key:
        return None
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
