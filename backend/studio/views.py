import io
import re
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction, connection, DatabaseError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from botocore.exceptions import ClientError
from django.shortcuts import get_object_or_404
from PIL import Image, UnidentifiedImageError
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from . import byteplus_assets, enhancer, media, provider, references, services, storage
from .models import Asset, Chunk, Project, Run, Wallet
from .serializers import ProjectConfig


def asset_data(asset):
    return {
        "id": str(asset.id),
        "name": asset.name,
        "kind": asset.kind,
        "url": f"/api/characters/{asset.id}/preview"
        if asset.provider_id
        else (f"/api/assets/{asset.id}/preview" if asset.mime.startswith("image/") else storage.url(asset.key)),
        "source": "byteplus" if asset.provider_id else "upload",
        "status": asset.provider_status if asset.provider_id else "Active",
        "duration": asset.duration,
        "category": asset.category,
        "description": asset.description,
        "analysisStatus": asset.analysis_status,
    }


def run_data(run):
    return {
        "id": str(run.id),
        "part": run.part,
        "state": run.state,
        "revision": run.snapshot.get("revision"),
        "url": storage.url(run.output_key),
        "downloadUrl": storage.url(run.output_key, download=True),
        "chunks": [
            {
                "id": str(c.id),
                "start": c.start,
                "duration": c.duration,
                "state": c.state,
                "error": c.error,
                "url": storage.url(c.output_key),
            }
            for c in run.chunks.all()
        ],
    }


def project_data(project):
    runs = []
    for part in ("freeform", "motion", "full"):
        run = project.runs.filter(part=part).order_by("-created").first()
        if run:
            if part == "full" and run.snapshot.get("sources") != [r["id"] for r in runs]:
                continue
            runs.append(run_data(run))
    return {
        "id": str(project.id),
        "name": project.name,
        "revision": project.revision,
        "config": project.config,
        "runs": runs,
    }


@api_view(["GET"])
def bootstrap(request):
    wallet, _ = Wallet.objects.get_or_create(user=request.user)
    return Response(
        {
            "user": {"id": request.user.id, "name": request.user.username},
            "wallet": {
                "available": wallet.available,
                "held": wallet.held,
                "spent": wallet.spent,
            },
            "assetCategories": references.CATEGORIES,
            "pricing": settings.CREDITS_PER_SECOND,
            "simulation": settings.GENERATION_PROVIDER == "mock",
            "projects": [
                {"id": str(p.id), "name": p.name}
                for p in Project.objects.filter(owner=request.user).order_by("-updated")
            ],
            "assets": [
                asset_data(a) for a in Asset.objects.filter(owner=request.user).order_by("-created")
            ],
            "history": [
                {"id": e.id, "kind": e.kind, "amount": e.amount, "created": e.created}
                for e in wallet.entries.order_by("-created")[:50]
            ],
        }
    )


@api_view(["POST"])
def projects(request):
    serializer = ProjectConfig(data={}, context={"user": request.user})
    serializer.is_valid(raise_exception=True)
    project = Project.objects.create(owner=request.user, config=serializer.validated_data)
    return Response(project_data(project), status=201)


@api_view(["GET", "PATCH"])
def project_detail(request, pk):
    if request.method == "GET":
        return Response(project_data(get_object_or_404(Project, id=pk, owner=request.user)))
    with transaction.atomic():
        project = get_object_or_404(
            Project.objects.select_for_update(of=("self",)), id=pk, owner=request.user
        )
        if request.data.get("revision") != project.revision:
            return Response(
                {"detail": "This project changed in another window. Reload before saving."},
                status=409,
            )
        serializer = ProjectConfig(
            data=request.data.get("config", {}), context={"user": request.user}
        )
        serializer.is_valid(raise_exception=True)
        name = str(request.data.get("name", project.name)).strip()
        if not 1 <= len(name) <= 120:
            raise ValidationError("Project name must be 1–120 characters.")
        project.name, project.config = name, serializer.validated_data
        project.revision += 1
        project.save()
    return Response(project_data(project))


@api_view(["POST"])
def generate(request, pk):
    get_object_or_404(Project, id=pk, owner=request.user)
    run = services.generate(
        request.user, pk, request.data.get("part"), request.data.get("requestKey")
    )
    return Response(run_data(run), status=202)


@api_view(["POST"])
def retry(request, pk):
    get_object_or_404(Chunk, id=pk, run__project__owner=request.user)
    services.retry(request.user, pk)
    return Response({"ok": True}, status=202)


@api_view(["POST"])
def export(request, pk):
    with transaction.atomic():
        project = get_object_or_404(
            Project.objects.select_for_update(of=("self",)), id=pk, owner=request.user
        )
        sources = []
        for part in ("freeform", "motion"):
            run = project.runs.filter(part=part).order_by("-created").first()
            if not run or run.state != "ready":
                raise ValidationError("Both parts must be ready before export.")
            sources.append(run)
        scene_keys = ("character", "clothing", "location", "duration")
        if any(sources[0].snapshot.get(k) != sources[1].snapshot.get(k) for k in scene_keys):
            raise ValidationError(
                "The two parts use different looks or timing. Regenerate the outdated part."
            )
        source_ids = [str(r.id) for r in sources]
        previous = project.runs.filter(part="full", snapshot__sources=source_ids).first()
        if previous:
            return Response(run_data(previous))
        run = Run.objects.create(
            project=project,
            part="full",
            snapshot={"sources": source_ids, "revision": project.revision},
            request_key=uuid.uuid4(),
        )
        index = 0
        for source in sources:
            for c in source.chunks.all():
                Chunk.objects.create(
                    run=run,
                    position=index,
                    start=c.start,
                    duration=c.duration,
                    prompt="",
                    state="ready",
                    output_key=c.output_key,
                    cost=0,
                )
                index += 1
    return Response(run_data(run), status=202)


@api_view(["POST"])
def upload(request):
    incoming = request.FILES.get("file")
    kind = request.data.get("kind")
    if not incoming or kind not in ("character", "clothing", "location", "motion"):
        raise ValidationError("Choose a file and its library.")
    category = request.data.get("category", "")
    if kind in references.CATEGORIES and (
        not isinstance(category, str) or (category and category not in references.CATEGORIES[kind])
    ):
        raise ValidationError("Choose a category for this reference.")
    if incoming.size > 50 * 1024 * 1024:
        raise ValidationError("Files must be at most 50 MB.")
    asset_id = uuid.uuid4()
    duration = 0
    if kind == "motion":
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input"
            with path.open("wb") as stream:
                for block in incoming.chunks():
                    stream.write(block)
            try:
                info = media.probe(path)
                duration = int(float(info["format"]["duration"]))
                if not 4 <= duration <= 120 or not any(
                    s["codec_type"] == "video" for s in info["streams"]
                ):
                    raise ValueError()
                # Remove metadata and extra streams from uploaded references.
                output = Path(folder) / "reference.mp4"
                media.command(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        str(path),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-map_metadata",
                        "-1",
                        "-c",
                        "copy",
                        str(output),
                    ],
                    60,
                )
            except Exception:
                raise ValidationError(
                    "Use a playable MP4 motion reference between 4 and 120 seconds."
                )
            key = f"{request.user.id}/assets/{asset_id}.mp4"
            mime = "video/mp4"
            with output.open("rb") as stream:
                storage.put(key, stream, mime)
    else:
        try:
            with Image.open(incoming) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError()
                image.load()
                image = image.convert("RGB")
                image.thumbnail((2048, 2048))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=90)
                buffer.seek(0)
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ):
            raise ValidationError("Use a valid image up to 25 megapixels.")
        key, mime = f"{request.user.id}/assets/{asset_id}.jpg", "image/jpeg"
        storage.put(key, buffer, mime)
    asset = Asset.objects.create(
        id=asset_id,
        owner=request.user,
        kind=kind,
        name=Path(incoming.name).stem[:120] or "Untitled",
        key=key,
        mime=mime,
        duration=duration,
        category=category if kind in references.CATEGORIES else "",
        analysis_status="pending" if kind in references.CATEGORIES else "",
    )
    if kind != "motion":
        buffer.seek(0)
        with Image.open(buffer) as preview_image:
            save_preview(asset, preview_image)
    warning = ""
    if kind in references.CATEGORIES:
        try:
            enhancer.analyze(asset)
        except enhancer.EnhancementError as exc:
            warning = str(exc)
    return Response({**asset_data(asset), "warning": warning}, status=201)


@api_view(["GET", "HEAD"])
def local_media(request, key):
    if not key.startswith(f"{request.user.id}/"):
        raise Http404
    if settings.S3_BUCKET:
        owned = (
            Asset.objects.filter(owner=request.user, key=key).exists()
            or Run.objects.filter(project__owner=request.user, output_key=key).exists()
            or Chunk.objects.filter(run__project__owner=request.user, output_key=key).exists()
        )
        if not owned:
            raise Http404
        params = {"Bucket": settings.S3_BUCKET, "Key": key}
        byte_range = request.headers.get("Range")
        if byte_range and request.method != "HEAD":
            if not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", byte_range):
                return HttpResponse(status=416)
            params["Range"] = byte_range
        try:
            result = (
                storage.client().head_object(**params)
                if request.method == "HEAD"
                else storage.client().get_object(**params)
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "AccessDenied"}:
                raise Http404 from None
            return HttpResponse(status=416 if code == "InvalidRange" else 502)
        if request.method == "HEAD":
            response = HttpResponse()
        else:
            body = result["Body"]

            def stream():
                try:
                    yield from body.iter_chunks(chunk_size=64 * 1024)
                finally:
                    body.close()

            response = StreamingHttpResponse(
                stream(), status=206 if "ContentRange" in result else 200
            )
            response._resource_closers.append(body.close)
        response["Content-Type"] = result.get("ContentType", "application/octet-stream")
        response["Content-Length"] = result["ContentLength"]
        response["Accept-Ranges"] = "bytes"
        if "ContentRange" in result:
            response["Content-Range"] = result["ContentRange"]
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.GET.get("download") == "1":
            response["Content-Disposition"] = 'attachment; filename="video.mp4"'
        return response
    if not settings.DEBUG:
        raise Http404
    path = (settings.MEDIA_ROOT / key).resolve()
    if not path.is_relative_to(settings.MEDIA_ROOT.resolve()) or not path.is_file():
        raise Http404
    return FileResponse(
        path.open("rb"),
        as_attachment=request.GET.get("download") == "1",
        filename="video.mp4" if path.suffix == ".mp4" else "reference.jpg",
        content_type="video/mp4" if path.suffix == ".mp4" else "image/jpeg",
    )


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"ok": False}, status=503)
    return JsonResponse({"ok": True})


def index(request):
    path = settings.BASE_DIR.parent / "frontend" / "dist" / "index.html"
    if not path.exists():
        return HttpResponse(
            "Build the frontend first: cd frontend && npm ci && npm run build",
            status=503,
        )
    return HttpResponse(path.read_text(), content_type="text/html")


@api_view(["POST"])
def characters(request):
    try:
        return Response([asset_data(a) for a in byteplus_assets.sync(request.user)])
    except byteplus_assets.CatalogError as exc:
        return Response({"detail": str(exc)}, status=502)


def preview_key(asset):
    # Source identity is immutable; version changes invalidate old renderings.
    import hashlib

    source = f"{asset.provider_project}:{asset.provider_id}:{asset.key}"
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    return f"{asset.owner_id}/previews/v1/{asset.id}-{digest}.jpg"


def save_preview(asset, image):
    image = image.copy()
    image.thumbnail((640, 640), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=78, optimize=True)
    data = buffer.getvalue()
    storage.put(preview_key(asset), io.BytesIO(data), "image/jpeg")
    return data


@api_view(["GET"])
def character_preview(request, pk):
    return asset_preview_response(request, pk, character_only=True)


@api_view(["GET"])
def asset_preview(request, pk):
    return asset_preview_response(request, pk)


def asset_preview_response(request, pk, character_only=False):
    asset = get_object_or_404(Asset, pk=pk, owner=request.user)
    if character_only and (asset.kind != "character" or not asset.provider_id):
        raise Http404()
    if not asset.provider_id and not asset.mime.startswith("image/"):
        raise Http404()
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preview"
            try:
                storage.fetch(preview_key(asset), path)
                data = path.read_bytes()
            except (FileNotFoundError, ClientError) as exc:
                if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") not in {
                    "NoSuchKey", "404", "403", "AccessDenied"
                }:
                    raise
                if asset.provider_id:
                    item = byteplus_assets.detail(asset)
                    if item.get("AssetType") != "Image" or item.get("Id") != asset.provider_id:
                        raise ValueError()
                    provider.download(
                        item.get("URL", ""), path,
                        allowed_hosts=["ark-media-asset-ap-southeast-1.tos-ap-southeast-1.volces.com"],
                        max_bytes=20 * 1024 * 1024,
                    )
                else:
                    storage.fetch(asset.key, path)
                with Image.open(path) as image:
                    if image.width * image.height > 25_000_000:
                        raise ValueError()
                    if asset.provider_id:
                        # Only the UI is cropped; generation keeps the original asset.
                        width, height = image.size
                        image = image.crop((width * 2 // 3, 0, width, max(1, height // 3)))
                    data = save_preview(asset, image)
        response = HttpResponse(data, content_type="image/jpeg")
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        return response
    except Exception:
        # Do not expose private upstream URLs or responses.
        return HttpResponse(status=502)


@api_view(["POST"])
def analyze_asset(request, pk):
    asset = get_object_or_404(Asset, pk=pk, owner=request.user, kind__in=references.CATEGORIES)
    category = request.data.get("category", asset.category)
    if not isinstance(category, str) or (
        category and category not in references.CATEGORIES[asset.kind]
    ):
        raise ValidationError("Choose a category for this reference.")
    try:
        enhancer.analyze(asset, category=category)
    except enhancer.EnhancementError as exc:
        return Response({"detail": str(exc), "asset": asset_data(asset)}, status=502)
    return Response(asset_data(asset))


@api_view(["POST"])
def reference_prompt(request):
    serializer = ProjectConfig(data=request.data, context={"user": request.user})
    serializer.is_valid(raise_exception=True)
    config = serializer.validated_data
    assets = references.ordered_assets(request.user, config)
    text = references.instructions(assets)
    return Response({"references": text, "categories": references.CATEGORIES})


@api_view(["POST"])
def enhance_prompt(request):
    serializer = ProjectConfig(data=request.data, context={"user": request.user})
    serializer.is_valid(raise_exception=True)
    try:
        return Response(enhancer.enhance(request.user, serializer.validated_data))
    except enhancer.EnhancementError as exc:
        return Response({"detail": str(exc)}, status=502)


@api_view(["PATCH", "DELETE"])
def edit_asset(request, pk):
    from datetime import timedelta
    from django.utils import timezone

    if request.method == "DELETE":
        return delete_asset(request, pk)

    with transaction.atomic():
        asset = get_object_or_404(
            Asset.objects.select_for_update(),
            pk=pk,
            owner=request.user,
            kind__in=references.CATEGORIES,
        )
        if (
            asset.analysis_status == "pending"
            and asset.analysis_started
            and asset.analysis_started > timezone.now() - timedelta(minutes=3)
        ):
            return Response({"detail": "Wait for the current analysis to finish."}, status=409)
        name, category, description = (
            request.data.get(key, getattr(asset, key))
            for key in ("name", "category", "description")
        )
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValidationError("Name must be 1–120 characters.")
        if not isinstance(category, str) or category not in references.CATEGORIES[asset.kind]:
            raise ValidationError("Choose a category for this reference.")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1200:
            raise ValidationError("Description must be 1–1200 characters.")
        if re.search(r"@(?:image|video)\d|https?://|asset://", name + description, re.I):
            raise ValidationError(
                "Reference details must describe the image without tags or links."
            )
        asset.name, asset.category, asset.description = name.strip(), category, description.strip()
        asset.analysis_status = "ready"
        asset.save(update_fields=["name", "category", "description", "analysis_status"])
    return Response(asset_data(asset))


@transaction.atomic
def delete_asset(request, pk):
    from datetime import timedelta
    from django.utils import timezone

    # Generation takes the same project lock before snapshotting references.
    projects = list(Project.objects.select_for_update().filter(owner=request.user).order_by("pk"))
    asset = get_object_or_404(
        Asset.objects.select_for_update(), pk=pk, owner=request.user,
        kind__in=("clothing", "location"), provider_id="",
    )
    if (asset.analysis_status == "pending" and asset.analysis_started
            and asset.analysis_started > timezone.now() - timedelta(minutes=3)):
        return Response({"detail": "Wait for the current analysis to finish."}, status=409)
    asset_id = str(asset.pk)
    for run in Run.objects.filter(project__owner=request.user).exclude(state="ready"):
        snapshot = run.snapshot
        ids = snapshot.get("reference_ids", []) + snapshot.get("clothing", [])
        if asset_id in ids or snapshot.get("location") == asset_id:
            return Response({"detail": "This asset is used by an unfinished generation. Finish it before deleting."}, status=409)
    changed = []
    for project in projects:
        config = project.config
        if asset_id not in config.get("clothing", []) and config.get("location") != asset_id:
            continue
        previous = project.revision
        project.config = {
            **config,
            "clothing": [item for item in config.get("clothing", []) if item != asset_id],
            "location": None if config.get("location") == asset_id else config.get("location"),
        }
        project.revision += 1
        project.save(update_fields=["config", "revision", "updated"])
        changed.append({"id": str(project.pk), "previousRevision": previous, "revision": project.revision})
    # Removing the ownership record also revokes browser access to the private files.
    asset.delete()
    return Response({"deleted": asset_id, "projects": changed})
