import io
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction, connection, DatabaseError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from PIL import Image, UnidentifiedImageError
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from . import media, services, storage
from .models import Asset, Chunk, Project, Run, Wallet
from .serializers import ProjectConfig


def asset_data(asset):
    return {
        "id": str(asset.id),
        "name": asset.name,
        "kind": asset.kind,
        "url": storage.url(asset.key),
        "duration": asset.duration,
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
    )
    return Response(asset_data(asset), status=201)


@api_view(["GET"])
def local_media(request, key):
    if not settings.DEBUG or settings.S3_BUCKET or not key.startswith(f"{request.user.id}/"):
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
