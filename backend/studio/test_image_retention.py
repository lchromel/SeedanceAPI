import base64
import io
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.test import APIClient

from . import image_retention, image_tasks, images, storage
from .models import Asset, ImageGeneration, Ledger, Wallet


class ExpiringRedis:
    """TTL runs independently of web/worker cleanup, with a controlled test clock."""

    def __init__(self):
        self.now = timezone.now()
        self.items = {}

    def set(self, key, value, *, ex):
        self.items[key] = (bytes(value), self.now + timedelta(seconds=ex))

    def get(self, key):
        item = self.items.get(key)
        if item and item[1] > self.now:
            return item[0]
        self.items.pop(key, None)
        return None

    def delete(self, *keys):
        for key in keys:
            self.items.pop(key, None)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        for key in list(self.items):
            self.get(key)


@override_settings(
    SAVE_IMAGES_TO_S3=False,
    S3_BUCKET="must-not-be-used",
    DEBUG=True,
    GENERATION_PROVIDER="mock",
    CREDITS_ENABLED=False,
    IMAGE_TEMP_REDIS_URL="redis://test-only",
    IMAGE_REFERENCE_TTL_SECONDS=60,
    IMAGE_DOWNLOAD_TTL_SECONDS=120,
)
class TemporaryImageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("temporary-owner")
        self.other = get_user_model().objects.create_user("temporary-outsider")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.cache = ExpiringRedis()
        for mocked in (
            patch.object(storage, "temporary_client", return_value=self.cache),
            patch.object(
                storage, "client", side_effect=AssertionError("Temporary images must not use S3")
            ),
            patch.object(timezone, "now", side_effect=lambda: self.cache.now),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def file(self):
        stream = io.BytesIO()
        Image.new("RGB", (160, 160), "green").save(stream, "PNG")
        stream.seek(0)
        stream.name = "reference.png"
        return stream

    def upload(self):
        response = self.client.post(
            "/api/assets", {"kind": "image", "file": self.file()}, format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return Asset.objects.get(pk=response.data["id"])

    def start(self, assets=(), key=None):
        response = self.client.post(
            "/api/images",
            {
                "prompt": "Use the green palette from Image 1 for a ceramic vase.",
                "references": [str(asset.pk) for asset in assets],
                "requestKey": str(key or uuid.uuid4()),
                "ratio": "4:3",
                "resolution": "1K",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return ImageGeneration.objects.get(pk=response.data["id"])

    def process(self, job):
        with self.captureOnCommitCallbacks(execute=True):
            image_tasks.process_image(job.pk)
        job.refresh_from_db()

    def test_upload_preview_and_source_are_private_without_s3(self):
        asset = self.upload()
        self.assertTrue(storage.is_temporary(asset.key))
        self.assertEqual(asset.expires_at, self.cache.now + timedelta(seconds=60))
        self.assertEqual(len(self.cache.items), 2)
        self.assertEqual(self.client.get(f"/api/assets/{asset.pk}/preview").status_code, 200)
        self.assertEqual(self.client.get(storage.url(asset.key)).status_code, 200)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/assets/{asset.pk}/preview").status_code, 404)
        self.assertEqual(self.client.get(storage.url(asset.key)).status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(storage.url(asset.key)).status_code, 403)

    def test_unused_references_expire_without_application_cleanup(self):
        asset = self.upload()
        self.cache.advance(61)
        self.assertFalse(self.cache.items)
        self.assertEqual(self.client.get(f"/api/assets/{asset.pk}/preview").status_code, 404)
        self.assertEqual(self.client.get(storage.url(asset.key)).status_code, 404)
        self.assertEqual(self.client.get("/api/bootstrap").data["assets"], [])
        with patch.object(images.enhancer, "chat") as chat:
            response = self.client.post(
                "/api/images/enhance",
                {
                    "prompt": "Use Image 1.",
                    "references": [str(asset.pk)],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        chat.assert_not_called()
        image_retention.expire_records()
        self.assertFalse(Asset.objects.exists())

    def test_generation_deletes_inputs_and_result_expires_with_private_downloads_revoked(self):
        first, second = self.upload(), self.upload()
        request_key = uuid.uuid4()
        job = self.start([first, second], request_key)
        self.assertEqual(len(self.cache.items), 6)
        self.process(job)
        self.assertEqual(job.state, "ready")
        self.assertEqual(job.expires_at, self.cache.now + timedelta(seconds=120))
        self.assertFalse(Asset.objects.exists())
        self.assertEqual(len(self.cache.items), 1)
        self.assertEqual(
            self.client.get(storage.url(job.output_key, download=True)).status_code, 200
        )
        self.assertEqual(self.client.get(f"/api/assets/{first.pk}/preview").status_code, 404)
        self.assertEqual(self.start([first, second], request_key).pk, job.pk)
        self.assertFalse(Ledger.objects.exists())
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(storage.url(job.output_key)).status_code, 404)
        self.client.force_authenticate(self.user)
        self.cache.advance(121)
        self.assertFalse(self.cache.items)
        self.assertEqual(
            self.client.get(storage.url(job.output_key, download=True)).status_code, 404
        )
        history = self.client.get("/api/images").data[0]
        self.assertEqual(history["state"], "expired")
        self.assertIsNone(history["url"])
        self.assertIsNone(history["downloadUrl"])
        image_retention.expire_records()
        job.refresh_from_db()
        self.assertEqual((job.state, job.output_key), ("expired", ""))

    def test_parallel_jobs_have_independent_copies_and_provider_gets_inline_images(self):
        asset = self.upload()
        source = storage.temporary_bytes(asset.key)
        first, second = self.start([asset]), self.start([asset])
        self.process(first)
        self.assertFalse(Asset.objects.exists())
        self.assertEqual(storage.temporary_bytes(second.snapshot["reference_keys"][0]), source)
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"url": "https://provider.example/output.jpg"}]}
        with patch.object(image_tasks.requests, "post", return_value=response) as post:
            image_tasks.submit(second)
        inline = post.call_args.kwargs["json"]["image"][0]
        self.assertTrue(inline.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(inline.split(",", 1)[1]), source)
        self.process(second)
        self.assertEqual(second.state, "ready")
        self.assertEqual(len(self.cache.items), 2)

    def test_settings_switch_keeps_original_job_retention_mode(self):
        job = self.start([self.upload()])
        with override_settings(SAVE_IMAGES_TO_S3=True, IMAGE_DOWNLOAD_TTL_SECONDS=500):
            self.process(job)
        self.assertTrue(storage.is_temporary(job.output_key))
        self.assertEqual(job.expires_at, self.cache.now + timedelta(seconds=120))

    def test_missing_queued_reference_fails_before_a_paid_provider_call(self):
        job = self.start([self.upload()])
        job.snapshot["provider"] = "byteplus"
        job.save()
        self.cache.advance(61)
        with patch.object(image_tasks.requests, "post") as post:
            self.process(job)
        self.assertEqual(job.state, "error")
        post.assert_not_called()
        self.assertFalse(Asset.objects.exists())

    def test_unknown_submission_deletes_temporary_references(self):
        job = self.start([self.upload()])
        job.snapshot["provider"] = "byteplus"
        job.save()
        with patch.object(image_tasks, "submit", side_effect=TimeoutError()):
            self.process(job)
        self.assertEqual(job.state, "review")
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(self.cache.items)

    @override_settings(CREDITS_ENABLED=True, CREDITS_PER_IMAGE=1)
    def test_failed_copy_rolls_back_job_and_reservation_and_deletes_partial_copies(self):
        first, second = self.upload(), self.upload()
        Wallet.objects.create(user=self.user, available=10)
        source = storage.temporary_bytes(first.key)
        with patch.object(storage, "temporary_bytes", side_effect=[source, FileNotFoundError()]):
            response = self.client.post(
                "/api/images",
                {
                    "prompt": "Use Image 1 and Image 2.",
                    "references": [str(first.pk), str(second.pk)],
                    "requestKey": str(uuid.uuid4()),
                },
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageGeneration.objects.exists())
        self.assertFalse(Ledger.objects.exists())
        self.assertEqual(Wallet.objects.get(user=self.user).available, 10)
        self.assertEqual(len(self.cache.items), 4)

    def test_unavailable_temporary_store_never_falls_back_to_s3(self):
        with patch.object(storage, "temporary_client", side_effect=RedisConnectionError()):
            response = self.client.post(
                "/api/assets", {"kind": "image", "file": self.file()}, format="multipart"
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Asset.objects.exists())

    @override_settings(
        DEBUG=False,
        GENERATION_PROVIDER="byteplus",
        ARK_API_KEY="test-only",
        S3_BUCKET="",
        OUTPUT_HOSTS=["provider.example"],
    )
    def test_temporary_generation_does_not_require_an_s3_bucket(self):
        self.assertTrue(images.configuration()["generationEnabled"])
