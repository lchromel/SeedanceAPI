import base64
import io
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from . import enhancer, image_tasks, replacements, storage
from .models import Asset, ImageGeneration, Ledger, Wallet
from .test_image_retention import ExpiringRedis


@override_settings(
    DEBUG=True,
    GENERATION_PROVIDER="mock",
    CREDITS_ENABLED=False,
    S3_BUCKET="",
    SAVE_IMAGES_TO_S3=True,
)
class ReplacementTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        configured = override_settings(MEDIA_ROOT=Path(folder.name))
        configured.enable()
        self.addCleanup(configured.disable)
        self.user = get_user_model().objects.create_user("series-owner")
        self.other = get_user_model().objects.create_user("series-outsider")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def upload(self, name, size=(160, 240)):
        stream = io.BytesIO()
        Image.new("RGB", size, "red").save(stream, "PNG")
        stream.seek(0)
        stream.name = name + ".png"
        response = self.client.post(
            "/api/assets", {"kind": "image", "file": stream}, format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return Asset.objects.get(pk=response.data["id"])

    def data(self, originals, replacements_by_role=None, request_key=None):
        return {
            "originals": [
                {
                    "asset": str(asset.pk),
                    "prompt": f"Edit Image 1 for photograph {i + 1}. Preserve its composition.",
                    "notes": f"Wish {i + 1}",
                }
                for i, asset in enumerate(originals)
            ],
            "replacements": {
                role: str(asset.pk) for role, asset in (replacements_by_role or {}).items()
            },
            "instructions": "Keep the atmosphere",
            "ratio": "original",
            "resolution": "1K",
            "requestKey": str(request_key or uuid.uuid4()),
        }

    def generate(self, data):
        response = self.client.post("/api/images/replacements/generate", data, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def test_each_original_has_its_own_prompt_job_order_and_reference_set(self):
        portrait = self.upload("portrait")
        landscape = self.upload("landscape", (320, 180))
        model, clothing = self.upload("new-model"), self.upload("outfit")
        data = self.data([portrait, landscape], {"model": model, "clothing": clothing})
        with patch.object(enhancer, "chat") as chat:
            response = self.generate(data)
        chat.assert_not_called()
        self.assertEqual(len(response.data), 2)
        jobs = [ImageGeneration.objects.get(pk=item["id"]) for item in response.data]
        self.assertNotEqual(jobs[0].request_key, jobs[1].request_key)
        for i, (job, original) in enumerate(zip(jobs, [portrait, landscape])):
            self.assertEqual(job.snapshot["prompt"], data["originals"][i]["prompt"])
            self.assertEqual(
                job.snapshot["references"], [str(original.pk), str(model.pk), str(clothing.pk)]
            )
            self.assertEqual(job.snapshot["original_id"], str(original.pk))
            self.assertEqual(job.snapshot["original_index"], i)
            self.assertEqual(job.snapshot["mode"], "replacement")
            self.assertEqual(job.snapshot["individual_wishes"], f"Wish {i + 1}")
        self.assertEqual(jobs[0].snapshot["ratio"], "2:3")
        self.assertEqual(jobs[1].snapshot["ratio"], "16:9")
        for job in jobs:
            with self.captureOnCommitCallbacks(execute=True):
                image_tasks.process_image(job.pk)
            job.refresh_from_db()
            self.assertEqual(job.state, "ready")
        self.assertFalse(Ledger.objects.exists())

    def test_repeat_batch_keeps_same_jobs_even_if_originals_are_no_longer_available(self):
        originals = [self.upload("one"), self.upload("two")]
        data = self.data(originals)
        first = self.generate(data)
        Asset.objects.filter(pk__in=[asset.pk for asset in originals]).delete()
        repeated = self.generate(data)
        self.assertEqual(
            [item["id"] for item in first.data], [item["id"] for item in repeated.data]
        )
        self.assertEqual(ImageGeneration.objects.count(), 2)

    def test_two_individual_enhancements_analyze_only_their_own_original(self):
        first, second = self.upload("portrait"), self.upload("landscape", (320, 180))
        makeup = self.upload("makeup")
        for original, size, scene in (
            (first, (160, 240), "Portrait in soft side light"),
            (second, (320, 180), "Wide street composition"),
        ):
            text = f"Edit Image 1: {scene}. Apply only the makeup from Image 2; preserve the original person, clothing and location."
            with patch.object(
                enhancer,
                "chat",
                side_effect=[
                    json.dumps(
                        {
                            "images": [
                                {"image": 1, "description": scene},
                                {"image": 2, "description": "Green eye shadow"},
                            ]
                        }
                    ),
                    text,
                ],
            ) as chat:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        "replacements": {"makeup": str(makeup.pk)},
                        "instructions": "Улучши свет",
                        "notes": "Сохрани выражение",
                        "ratio": "original",
                    },
                    format="json",
                )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["prompt"], text)
            context = json.loads(chat.call_args_list[1].args[2])
            self.assertEqual(context["replacementPlan"]["makeup"]["source"], "Image 2")
            for role in ("model", "clothing", "location"):
                self.assertEqual(context["replacementPlan"][role]["source"], "original")
            self.assertEqual(context["individualWishes"], "Сохрани выражение")
            self.assertEqual(context["revisionInstructions"], "Улучши свет")
            content = chat.call_args_list[0].args[2]
            self.assertEqual(len(content), 3)
            image = content[1]["image_url"]["url"]
            with Image.open(io.BytesIO(base64.b64decode(image.split(",", 1)[1]))) as source:
                self.assertEqual(source.size, size)
        self.assertFalse(ImageGeneration.objects.exists())

    def test_omitted_roles_preserve_original_and_reused_reference_is_sent_once(self):
        original, donor = self.upload("original"), self.upload("donor")
        plan = replacements.plan_for(
            str(original.pk), {"model": str(donor.pk), "makeup": str(donor.pk)}
        )
        self.assertEqual(plan["model"]["source"], "Image 2")
        self.assertEqual(plan["makeup"]["source"], "Image 2")
        self.assertEqual(plan["clothing"]["source"], "original")
        self.assertEqual(plan["location"]["source"], "original")
        response = self.generate(self.data([original], {"model": donor, "makeup": donor}))
        self.assertEqual(response.data[0]["references"], [str(original.pk), str(donor.pk)])
        prompt = replacements.base_prompt(plan)
        self.assertIn("Preserve the clothing and accessories from Image 1", prompt)
        self.assertIn("Preserve the location and background from Image 1", prompt)

    def test_foreign_references_duplicates_and_oversized_series_cannot_reach_provider(self):
        original, foreign = self.upload("original"), self.upload("private")
        foreign.owner = self.other
        foreign.save()
        cases = [
            self.data([original], {"model": foreign}),
            self.data([foreign]),
            self.data([original, original]),
        ]
        oversized = self.data([original])
        oversized["originals"] *= 51
        cases.append(oversized)
        with patch.object(enhancer, "chat") as chat, patch.object(image_tasks, "submit") as submit:
            for data in cases:
                response = self.client.post(
                    "/api/images/replacements/generate", data, format="json"
                )
                self.assertEqual(response.status_code, 400)
            response = self.client.post(
                "/api/images/replacements/prompt",
                {"original": str(original.pk), "replacements": {"location": str(foreign.pk)}},
                format="json",
            )
            self.assertEqual(response.status_code, 400)
        chat.assert_not_called()
        submit.assert_not_called()
        self.assertFalse(ImageGeneration.objects.exists())

    @override_settings(CREDITS_ENABLED=True, CREDITS_PER_IMAGE=1)
    def test_insufficient_credit_rolls_back_the_entire_series(self):
        data = self.data([self.upload("one"), self.upload("two")])
        Wallet.objects.create(user=self.user, available=1)
        response = self.client.post("/api/images/replacements/generate", data, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageGeneration.objects.exists())
        self.assertFalse(Ledger.objects.exists())
        self.assertEqual(Wallet.objects.get(user=self.user).available, 1)

    @override_settings(
        SAVE_IMAGES_TO_S3=False,
        IMAGE_TEMP_REDIS_URL="redis://test-only",
        S3_BUCKET="must-not-be-used",
    )
    def test_temporary_shared_references_survive_until_all_jobs_have_copies(self):
        cache = ExpiringRedis()
        with (
            patch.object(storage, "temporary_client", return_value=cache),
            patch.object(storage, "client", side_effect=AssertionError("S3 must not be used")),
        ):
            first, second, donor = self.upload("one"), self.upload("two"), self.upload("model")
            data = self.data([first, second], {"model": donor})
            response = self.generate(data)
            jobs = [ImageGeneration.objects.get(pk=item["id"]) for item in response.data]
            with self.captureOnCommitCallbacks(execute=True):
                image_tasks.process_image(jobs[0].pk)
            self.assertFalse(Asset.objects.filter(pk=donor.pk).exists())
            self.assertTrue(storage.temporary_bytes(jobs[1].snapshot["reference_keys"][1]))
            with self.captureOnCommitCallbacks(execute=True):
                image_tasks.process_image(jobs[1].pk)
            self.assertFalse(Asset.objects.exists())
            self.assertEqual(len(cache.items), 2)
            repeated = self.generate(data)
            self.assertEqual(
                [item["id"] for item in response.data], [item["id"] for item in repeated.data]
            )

    @override_settings(
        SAVE_IMAGES_TO_S3=False,
        IMAGE_TEMP_REDIS_URL="redis://test-only",
        S3_BUCKET="must-not-be-used",
    )
    def test_partial_temporary_series_rollback_removes_all_job_copies(self):
        cache = ExpiringRedis()
        with patch.object(storage, "temporary_client", return_value=cache):
            first, second, donor = self.upload("one"), self.upload("two"), self.upload("model")
            original_read = storage.temporary_bytes

            def read(key):
                if key == second.key:
                    raise FileNotFoundError()
                return original_read(key)

            with patch.object(storage, "temporary_bytes", side_effect=read):
                response = self.client.post(
                    "/api/images/replacements/generate",
                    self.data([first, second], {"model": donor}),
                    format="json",
                )
            self.assertEqual(response.status_code, 400)
            self.assertFalse(ImageGeneration.objects.exists())
            self.assertEqual(len(cache.items), 6)
