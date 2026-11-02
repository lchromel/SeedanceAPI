from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import enhancer, images, storage
from .models import ImageGeneration


def image_data(job):
    return {
        "id": str(job.id),
        "state": job.state,
        "prompt": job.snapshot["prompt"],
        "references": job.snapshot["references"],
        "ratio": job.snapshot["ratio"],
        "resolution": job.snapshot["resolution"],
        "size": job.snapshot["size"],
        "simulation": job.snapshot["provider"] == "mock",
        "created": job.created,
        "cost": job.cost,
        "error": job.error,
        "url": storage.url(job.output_key),
        "downloadUrl": storage.url(job.output_key, download=True),
    }


@api_view(["GET", "POST"])
def generations(request):
    if request.method == "GET":
        # Include every active job even when there is a long history of finished jobs.
        jobs = ImageGeneration.objects.filter(owner=request.user)
        active = list(jobs.exclude(state__in=["ready", "error"]))
        finished = list(jobs.filter(state__in=["ready", "error"])[:50])
        return Response(
            [
                image_data(job)
                for job in sorted(active + finished, key=lambda job: job.created, reverse=True)
            ]
        )
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
