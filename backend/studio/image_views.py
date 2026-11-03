from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.fields import UUIDField
from django.utils import timezone

from . import enhancer, images, storage
from .models import ImageGeneration


def image_data(job):
    expired = bool(job.expires_at and job.expires_at <= timezone.now())
    output_key = "" if expired else job.output_key
    return {
        "id": str(job.id),
        "state": "expired" if expired else job.state,
        "prompt": job.snapshot["prompt"],
        "references": job.snapshot["references"],
        "ratio": job.snapshot["ratio"],
        "resolution": job.snapshot["resolution"],
        "size": job.snapshot["size"],
        "simulation": job.snapshot["provider"] == "mock",
        "created": job.created,
        "cost": job.cost,
        "error": "This temporary image has been deleted. Generate a new image to continue."
        if expired
        else job.error,
        "url": storage.url(output_key),
        "downloadUrl": storage.url(output_key, download=True),
        "expiresAt": job.expires_at,
        "temporary": job.snapshot.get("temporary_output", False),
    }


@api_view(["GET", "POST"])
def generations(request):
    if request.method == "GET":
        # Include every active job even when there is a long history of finished jobs.
        jobs = ImageGeneration.objects.filter(owner=request.user)
        active = list(jobs.exclude(state__in=["ready", "error", "expired"]))
        finished = list(jobs.filter(state__in=["ready", "error", "expired"])[:50])
        return Response(
            [
                image_data(job)
                for job in sorted(active + finished, key=lambda job: job.created, reverse=True)
            ]
        )
    # A completed temporary job may already have deleted its source references.
    if isinstance(request.data, dict) and request.data.get("requestKey"):
        request_key = UUIDField().run_validation(request.data["requestKey"])
        previous = ImageGeneration.objects.filter(
            owner=request.user, request_key=request_key
        ).first()
        if previous:
            return Response(image_data(previous), status=201)
    serializer = images.ImageRequest(data=request.data, context={"user": request.user})
    serializer.is_valid(raise_exception=True)
    job = images.generate(request.user, serializer.validated_data)
    return Response(image_data(job), status=201)


@api_view(["POST"])
def enhance_prompt(request):
    serializer = images.ImageInput(data=request.data, context={"user": request.user})
    serializer.is_valid(raise_exception=True)
    try:
        return Response(images.enhance(request.user, serializer.validated_data))
    except enhancer.EnhancementError as exc:
        return Response({"detail": str(exc)}, status=502)
