import io
import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
from botocore.stub import Stubber
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from . import enhancer, images, image_tasks, provider, storage
from .models import Asset, ImageGeneration, Ledger, Wallet
from .tasks import dispatch


@override_settings(GENERATION_PROVIDER="mock", CREDITS_PER_IMAGE=3, DEBUG=True, S3_BUCKET="")
class ImageTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.folder.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = get_user_model().objects.create_user("image-maker")
        self.other = get_user_model().objects.create_user("other-image-maker")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        Wallet.objects.create(user=self.user, available=30)
        self.data = {
            "prompt": "A red ceramic cup on a pale table.",
            "references": [],
            "ratio": "16:9",
            "resolution": "2K",
            "requestKey": str(uuid.uuid4()),
        }

    def start(self, **changes):
        return self.client.post("/api/images", {**self.data, **changes}, format="json")

    def upload_image(self):
        stream = io.BytesIO()
        Image.new("RGB", (160, 160), "red").save(stream, "PNG")
        stream.seek(0)
        stream.name = "cup.png"
        return self.client.post(
            "/api/assets", {"kind": "image", "file": stream}, format="multipart"
        )

    @override_settings(S3_BUCKET="test-only-private-bucket")
    def test_s3_upload_keeps_preview_source_after_the_transfer_closes_its_stream(self):
        # Use the actual boto3 transfer implementation: upload_fileobj closes Body.
        # A local filesystem or a mocked storage.put does not reproduce this failure.
        s3 = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test-only",
            aws_secret_access_key="test-only",
        )
        for kind in ("image", "clothing", "location"):
            with self.subTest(kind=kind), Stubber(s3) as stub:
                stub.add_response("put_object", {})
                stub.add_response("put_object", {})
                stream = io.BytesIO()
                Image.new("RGB", (160, 160), "red").save(stream, "JPEG")
                stream.seek(0)
                stream.name = "reference.jpeg"
                with (
                    patch.object(storage, "client", return_value=s3),
                    patch.object(enhancer, "analyze"),
                ):
                    response = self.client.post(
                        "/api/assets", {"kind": kind, "file": stream}, format="multipart"
                    )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["kind"], kind)
                self.assertIn("/preview", response.data["url"])
                self.assertTrue(
                    Asset.objects.filter(pk=response.data["id"], owner=self.user).exists()
                )
                stub.assert_no_pending_responses()

    def test_authenticated_and_owner_scoped_history_and_media(self):
        job = self.start().data
        image_tasks.process_image(job["id"])
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/images").data, [])
        self.assertEqual(
            self.client.get(f"/api/media/{self.user.id}/images/{job['id']}.jpg").status_code, 404
        )
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/images").status_code, 403)

    def test_reservation_idempotency_and_simulation_settles_once(self):
        first = self.start()
        self.assertEqual(first.status_code, 201)
        second = self.start()
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(ImageGeneration.objects.count(), 1)
        self.assertEqual(Wallet.objects.get(user=self.user).held, 3)
        with patch.object(image_tasks, "submit") as submit:
            image_tasks.process_image(first.data["id"])
            image_tasks.process_image(first.data["id"])
            submit.assert_not_called()
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual((wallet.available, wallet.held, wallet.spent), (27, 0, 3))
        self.assertEqual(Ledger.objects.filter(kind="settle").count(), 1)
        job = ImageGeneration.objects.get()
        with Image.open(Path(self.folder.name) / job.output_key) as image:
            self.assertEqual(f"{image.width}x{image.height}", job.snapshot["size"])
        self.assertTrue(self.client.get("/api/images").data[0]["simulation"])

    def test_insufficient_credit_or_unconfigured_generation_creates_no_job(self):
        Wallet.objects.filter(user=self.user).update(available=2)
        self.assertEqual(self.start().status_code, 400)
        self.assertFalse(ImageGeneration.objects.exists())
        with override_settings(CREDITS_PER_IMAGE=None):
            self.assertEqual(self.start().status_code, 400)
        self.assertFalse(Ledger.objects.exists())

    def test_validation_precedes_submission(self):
        other = Asset.objects.create(
            owner=self.other, kind="image", name="private", mime="image/jpeg"
        )
        with patch.object(image_tasks, "submit") as submit:
            for changes in (
                {"references": [str(other.id)]},
                {"references": [str(uuid.uuid4())] * 11},
                {"ratio": "2:1"},
                {"resolution": "4K"},
                {"prompt": " "},
            ):
                self.assertEqual(self.start(**changes).status_code, 400)
            submit.assert_not_called()
        self.assertFalse(ImageGeneration.objects.exists())

    def test_upload_is_sanitized_and_references_preserve_order(self):
        with patch.object(enhancer, "chat") as chat:
            one, two = self.upload_image(), self.upload_image()
            chat.assert_not_called()
        self.assertEqual(one.status_code, 201)
        ids = [two.data["id"], one.data["id"]]
        response = self.start(references=ids)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ImageGeneration.objects.get().snapshot["references"], ids)
        self.assertEqual(self.start(references=[ids[0], ids[0]]).status_code, 400)

    def test_all_output_dimensions_satisfy_pro_limits(self):
        for resolution in images.RESOLUTIONS:
            for ratio in images.RATIOS:
                width, height = map(int, images.dimensions(resolution, ratio).split("x"))
                self.assertGreaterEqual(width * height, 921600)
                self.assertLessEqual(width * height, 4624220)
                self.assertAlmostEqual(
                    width / height, images.RATIOS[ratio][0] / images.RATIOS[ratio][1], delta=0.02
                )

    def test_reference_vision_then_deepseek_receives_observations_and_full_prompt(self):
        asset = self.upload_image().data
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"A bright red cup."}]}',
                "Place the cup from Image 1 on a pale table in soft window light.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/enhance",
                {
                    **self.data,
                    "references": [asset["id"]],
                    "instructions": "Сохрани красный цвет",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            chat.call_args_list[0]
            .args[2][1]["image_url"]["url"]
            .startswith("data:image/jpeg;base64,")
        )
        context = json.loads(chat.call_args_list[1].args[2])
        self.assertEqual(context["referenceObservations"][0]["description"], "A bright red cup.")
        self.assertIn("Сохрани", context["revisionInstructions"])
        self.assertEqual(
            response.data["prompt"],
            "Place the cup from Image 1 on a pale table in soft window light.",
        )
        self.assertFalse(ImageGeneration.objects.exists())
        self.assertFalse(Ledger.objects.exists())

    def test_enhance_rejects_incomplete_observations_or_wrong_reference_numbers(self):
        asset = self.upload_image().data
        for results in (
            ['{"images":[]}'],
            [
                '{"images":[{"image":1,"description":"Red cup"}]}',
                "Use Image 2 for the cup.",
                "Use Image 2 for the cup.",
            ],
        ):
            with patch.object(enhancer, "chat", side_effect=results):
                response = self.client.post(
                    "/api/images/enhance",
                    {
                        **self.data,
                        "references": [asset["id"]],
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, 502)

    def test_enhancement_accepts_unambiguous_reference_formats_for_two_image_edits(self):
        refs = [self.upload_image().data["id"], self.upload_image().data["id"]]
        vision = json.dumps(
            {
                "images": [
                    {"image": 1, "description": "A person standing in a sunlit street."},
                    {"image": 2, "description": "A front-facing portrait of an adult man."},
                ]
            }
        )
        for prompt in (
            "Edit the first image using the man's face from the second image. Preserve the scene.",
            "Replace the face in Image#1 with the face from Image#2. Keep the pose and lighting.",
            "Edit image1 using image2 as the facial reference. Keep the original composition.",
            "Use Image #1 as the base photograph and @Image2 as the identity reference.",
            "Use photo one as the scene and photo two for the face. Match the scene lighting.",
            "Use the 1st photograph as the base and the 2nd photograph for the face.",
        ):
            with (
                self.subTest(prompt=prompt),
                patch.object(enhancer, "chat", side_effect=[vision, prompt]),
            ):
                response = self.client.post(
                    "/api/images/enhance",
                    {
                        **self.data,
                        "references": refs,
                        "prompt": "замени лицо девушки на первой фотографии на мужчину со второй",
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["prompt"], prompt)

    def test_missing_reference_is_repaired_once_without_repeating_vision_or_losing_intent(self):
        refs = [self.upload_image().data["id"], self.upload_image().data["id"]]
        vision = json.dumps(
            {
                "images": [
                    {"image": 1, "description": "A person in a sunlit street."},
                    {"image": 2, "description": "A front-facing portrait of an adult man."},
                ]
            }
        )
        original = "замени лицо девушки на первой фотографии на мужчину со второй"
        full_prompt = "Edit Image 1 using the man's face from Image 2. Preserve the pose, clothing, street and lighting."
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                vision,
                "Replace the face in the base photograph with the supplied man's face.",
                full_prompt,
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/enhance",
                {
                    **self.data,
                    "references": refs,
                    "prompt": original,
                    "instructions": "Сохрани освещение",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["prompt"], full_prompt)
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(chat.call_args_list[0].args[0], images.settings.REFERENCE_VISION_MODEL)
        self.assertEqual(chat.call_args_list[1].args[0], images.settings.DEEPSEEK_MODEL)
        self.assertEqual(chat.call_args_list[2].args[0], images.settings.DEEPSEEK_MODEL)
        retry_context = json.loads(chat.call_args_list[2].args[2])
        self.assertEqual(retry_context["draft"], original)
        self.assertEqual(retry_context["revisionInstructions"], "Сохрани освещение")
        self.assertEqual(retry_context["referenceRepair"]["missingLabels"], ["Image 1", "Image 2"])
        self.assertEqual(retry_context["referenceObservations"], json.loads(vision)["images"])
        self.assertEqual(chat.call_args_list[2].kwargs["timeout"], (5, 15))
        self.assertFalse(ImageGeneration.objects.exists())
        self.assertFalse(Ledger.objects.exists())

    def test_nonexistent_ordinal_reference_still_fails_after_one_repair(self):
        asset = self.upload_image().data
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"A red cup."}]}',
                "Use the first photo and the third photo.",
                "Use Image1 and Image3 for the cup.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/enhance",
                {
                    **self.data,
                    "references": [asset["id"]],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(chat.call_count, 3)
        self.assertIn("unchanged", response.data["detail"])

    def test_text_only_enhancement_accepts_scene_numbers_and_rejects_hallucinated_images(self):
        with patch.object(
            enhancer, "chat", return_value="A room with 2 chairs and 3 lamps."
        ) as chat:
            response = self.client.post("/api/images/enhance", self.data, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(chat.call_count, 1)
        with patch.object(enhancer, "chat", return_value="Recreate the first image.") as chat:
            response = self.client.post("/api/images/enhance", self.data, format="json")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(chat.call_count, 2)

    def paid_job(self):
        response = self.start()
        job = ImageGeneration.objects.get(pk=response.data["id"])
        job.snapshot["provider"] = "byteplus"
        job.save()
        return job

    def test_confirmed_rejection_releases_credits_once(self):
        job = self.paid_job()
        with patch.object(image_tasks, "submit", side_effect=provider.Rejected("Rejected")):
            image_tasks.process_image(job.id)
            image_tasks.process_image(job.id)
        job.refresh_from_db()
        self.assertEqual(job.state, "error")
        self.assertEqual(Wallet.objects.get(user=self.user).available, 30)

    def test_uncertain_submission_never_resubmits_and_retains_reservation(self):
        job = self.paid_job()
        with patch.object(image_tasks, "submit", side_effect=TimeoutError()) as submit:
            image_tasks.process_image(job.id)
            image_tasks.process_image(job.id)
            self.assertEqual(submit.call_count, 1)
        job.refresh_from_db()
        self.assertEqual(job.state, "review")
        self.assertEqual(Wallet.objects.get(user=self.user).held, 3)

    def test_review_reconciliation_releases_only_a_confirmed_absent_result(self):
        job = self.paid_job()
        ImageGeneration.objects.filter(pk=job.id).update(state="review")
        call_command(
            "reconcile_image", str(job.id), confirmed_not_created=True, stdout=io.StringIO()
        )
        job.refresh_from_db()
        self.assertEqual(job.state, "error")
        self.assertEqual(Wallet.objects.get(user=self.user).available, 30)

    @override_settings(OUTPUT_HOSTS=["provider.example"])
    def test_review_reconciliation_resumes_a_confirmed_result_without_new_charge(self):
        job = self.paid_job()
        ImageGeneration.objects.filter(pk=job.id).update(state="review")
        call_command(
            "reconcile_image",
            str(job.id),
            output_url="https://provider.example/image.jpg",
            stdout=io.StringIO(),
        )
        job.refresh_from_db()
        self.assertEqual(job.state, "downloading")
        self.assertEqual(job.provider_output_url, "https://provider.example/image.jpg")
        self.assertEqual(Wallet.objects.get(user=self.user).held, 3)

    def test_download_recovery_does_not_resubmit_paid_request(self):
        job = self.paid_job()

        def download(url, target, **kwargs):
            Image.new("RGB", (1024, 1024), "blue").save(target, "JPEG")

        with (
            patch.object(
                image_tasks, "submit", return_value="https://provider.example/result.jpg"
            ) as submit,
            patch.object(provider, "download", side_effect=[TimeoutError(), None]) as fetch,
        ):
            image_tasks.process_image(job.id)
            job.refresh_from_db()
            self.assertEqual(job.state, "downloading")
            job.lease_until = timezone.now() - timedelta(seconds=1)
            job.save()
            fetch.side_effect = download
            image_tasks.process_image(job.id)
            self.assertEqual(submit.call_count, 1)
        job.refresh_from_db()
        self.assertEqual(job.state, "ready")
        self.assertEqual(job.provider_output_url, "")
        self.assertEqual(Wallet.objects.get(user=self.user).spent, 3)

    def test_dispatch_recovers_expired_submission_without_another_paid_call(self):
        job = self.paid_job()
        ImageGeneration.objects.filter(pk=job.id).update(
            state="generating",
            lease_until=timezone.now() - timedelta(seconds=1),
        )
        with patch.object(image_tasks.process_image, "delay") as send:
            dispatch()
            send.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "review")

    @override_settings(ARK_API_KEY="test-only")
    def test_provider_payload_uses_pro_model_ordered_references_and_supported_fields(self):
        job = self.paid_job()
        job.snapshot["reference_keys"] = ["first", "second"]
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"url": "https://provider.example/result.jpg"}]}
        with (
            patch.object(image_tasks.requests, "post", return_value=response) as call,
            patch.object(
                storage, "url", side_effect=lambda key, **kw: "https://private.example/" + key
            ),
        ):
            self.assertEqual(image_tasks.submit(job), "https://provider.example/result.jpg")
        payload = call.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "dola-seedream-5-0-pro-260628")
        self.assertEqual(
            payload["image"], ["https://private.example/first", "https://private.example/second"]
        )
        self.assertNotIn("sequential_image_generation", payload)
        self.assertNotIn("stream", payload)
        self.assertNotIn("n", payload)
