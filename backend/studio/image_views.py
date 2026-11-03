from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.fields import UUIDField
from django.utils import timezone
from django.shortcuts import get_object_or_404

from . import enhancer, image_retries, images, replacements, storage
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
        "mode": job.snapshot.get("mode", "single"),
        "batchId": job.snapshot.get("batch_id"),
        "originalId": job.snapshot.get("original_id"),
        "originalName": job.snapshot.get("original_name"),
        "originalIndex": job.snapshot.get("original_index"),
        "regeneratedFrom": job.snapshot.get("regenerated_from"),
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


@api_view(["POST"])
def enhance_replacement(request):
    serializer = replacements.ReplacementPrompt(data=request.data, context={"user": request.user})
    serializer.is_valid(raise_exception=True)
    try:
        return Response(replacements.enhance(request.user, serializer.validated_data))
    except enhancer.EnhancementError as exc:
        return Response({"detail": str(exc)}, status=502)


@api_view(["POST"])
def replacement_batch(request):
    if isinstance(request.data, dict) and request.data.get("requestKey"):
        request_key = UUIDField().run_validation(request.data["requestKey"])
        previous = replacements.existing_batch(request.user, request_key)
        if previous:
            return Response([image_data(job) for job in previous], status=201)
    serializer = replacements.ReplacementBatch(data=request.data, context={"user": request.user})
    serializer.is_valid(raise_exception=True)
    jobs = replacements.generate(request.user, serializer.validated_data)
    return Response([image_data(job) for job in jobs], status=201)


@api_view(["POST"])
def regenerate_image(request, pk):
    source = get_object_or_404(ImageGeneration, pk=pk, owner=request.user)
    serializer = image_retries.Regeneration(data=request.data)
    serializer.is_valid(raise_exception=True)
    job = image_retries.regenerate(request.user, source, serializer.validated_data)
    return Response(image_data(job), status=201)
