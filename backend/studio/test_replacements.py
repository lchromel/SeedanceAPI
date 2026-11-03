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
            self.assertEqual(
                job.snapshot["prompt"],
                data["originals"][i]["prompt"] + "\n\n" + replacements.TATTOO_INSTRUCTIONS[False],
            )
            self.assertFalse(job.snapshot["keep_tattoos"])
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
            self.assertEqual(
                response.data["prompt"], text + "\n\n" + replacements.TATTOO_INSTRUCTIONS[False]
            )
            context = json.loads(chat.call_args_list[1].args[2])
            self.assertEqual(context["replacementPlan"]["makeup"]["source"], "Image 2")
            for role in ("model", "clothing", "location"):
                self.assertEqual(context["replacementPlan"][role]["source"], "original")
            self.assertEqual(context["individualWishes"], "Сохрани выражение")
            self.assertEqual(context["revisionInstructions"], "Улучши свет")
            content = chat.call_args_list[0].args[2]
            vision_images = [item for item in content if item["type"] == "image_url"]
            self.assertEqual(len(vision_images), 2)
            image = vision_images[0]["image_url"]["url"]
            with Image.open(io.BytesIO(base64.b64decode(image.split(",", 1)[1]))) as source:
                self.assertEqual(source.size, size)
        self.assertFalse(ImageGeneration.objects.exists())

    def test_shared_location_is_observed_and_staged_against_each_own_original(self):
        portrait, wide = self.upload("portrait"), self.upload("wide", (320, 180))
        model, location = self.upload("model"), self.upload("courtyard")
        environment = "An ochre courtyard with stone paving, blue doors and a shaded arcade."
        for original, observation, staging in (
            (
                portrait,
                "Eye-level tight portrait; face at right, shoulders cropped.",
                "Show a softly focused ochre wall behind the shoulders, with a cropped blue doorway at left.",
            ),
            (
                wide,
                "Low camera full-body view; person standing at left, feet on ground.",
                "Show stone paving under the feet, a blue doorway and the arcade receding behind the standing figure.",
            ),
        ):
            prompt = f"Edit Image 1: {observation} Use the identity from Image 2. Rebuild the observed environment from Image 3: {staging}"
            with patch.object(
                enhancer,
                "chat",
                side_effect=[
                    json.dumps(
                        {
                            "images": [
                                {"image": 1, "description": observation},
                                {"image": 2, "description": "A person in a white shirt."},
                                {"image": 3, "description": environment},
                            ]
                        }
                    ),
                    prompt,
                ],
            ) as chat:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        "replacements": {"model": str(model.pk), "location": str(location.pk)},
                    },
                    format="json",
                )
            self.assertEqual(response.status_code, 200, response.data)
            texts = [
                item["text"] for item in chat.call_args_list[0].args[2] if item["type"] == "text"
            ]
            self.assertTrue(any("Image 1:" in text and "camera height" in text for text in texts))
            self.assertTrue(
                any(
                    "Image 3:" in text and "Location reference" in text and "materials" in text
                    for text in texts
                )
            )
            context = json.loads(chat.call_args_list[1].args[2])
            self.assertEqual(context["replacementPlan"]["location"]["source"], "Image 3")
            self.assertEqual(context["referenceObservations"][0]["description"], observation)
            self.assertEqual(context["referenceObservations"][2]["description"], environment)
            self.assertIn(staging, response.data["prompt"])
        self.assertFalse(ImageGeneration.objects.exists())

    def test_location_analysis_recovery_keeps_its_environment_focus(self):
        original, location = self.upload("original"), self.upload("location")
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"Tight eye-level portrait."}]}',
                "Ochre walls, blue doors and stone paving in a courtyard.",
                "Edit Image 1, keeping its tight portrait crop. Use the courtyard in Image 2: an ochre wall and the edge of a blue door behind the shoulders.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"location": str(location.pk)},
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        recovery = chat.call_args_list[1].args[2]
        self.assertIn("This is Image 2", recovery[0]["text"])
        self.assertIn("Location reference", recovery[0]["text"])
        self.assertIn("unseen areas unspecified", recovery[0]["text"])
        self.assertEqual(len([item for item in recovery if item["type"] == "image_url"]), 1)
        context = json.loads(chat.call_args_list[2].args[2])
        self.assertIn("blue doors", context["referenceObservations"][1]["description"])

    def test_tattoo_switch_reaches_writer_and_generation_and_replaces_previous_suffix(self):
        original = self.upload("original")
        removal = replacements.TATTOO_INSTRUCTIONS[False]
        preservation = replacements.TATTOO_INSTRUCTIONS[True]
        for keep in (False, True):
            previous = (
                "Edit Image 1. Preserve the pose.\n\n" + replacements.TATTOO_INSTRUCTIONS[not keep]
            )
            with patch.object(
                enhancer,
                "chat",
                side_effect=[
                    '{"images":[{"image":1,"description":"Person with a floral forearm tattoo."}]}',
                    previous,
                ],
            ) as chat:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        "prompt": previous,
                        "keepTattoos": keep,
                        "instructions": "Keep tattoos" if not keep else "Remove tattoos",
                    },
                    format="json",
                )
            self.assertEqual(response.status_code, 200, response.data)
            expected = preservation if keep else removal
            opposite = removal if keep else preservation
            context = json.loads(chat.call_args_list[1].args[2])
            self.assertEqual(context["tattooPolicy"], expected)
            self.assertEqual(response.data["prompt"].count(expected), 1)
            self.assertNotIn(opposite, response.data["prompt"])
            data = self.data([original])
            data["keepTattoos"] = keep
            data["originals"][0]["prompt"] = response.data["prompt"]
            result = self.generate(data)
            job = ImageGeneration.objects.get(pk=result.data[0]["id"])
            self.assertIs(job.snapshot["keep_tattoos"], keep)
            self.assertEqual(job.snapshot["prompt"], response.data["prompt"])

    def test_tattoo_suffix_cannot_push_a_provider_prompt_over_the_limit(self):
        original = self.upload("original")
        data = self.data([original])
        data["originals"][0]["prompt"] = "x" * 6000
        response = self.client.post("/api/images/replacements/generate", data, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageGeneration.objects.exists())

    def test_preserved_tattoos_are_observed_on_model_even_when_it_is_also_location(self):
        original, donor = self.upload("original"), self.upload("model-in-courtyard")
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"Tight portrait."},{"image":2,"description":"Model with a blue bird tattoo on the left forearm, in an ochre courtyard."}]}',
                "Edit Image 1. Use the person from Image 2 and restage its ochre courtyard as a softly focused wall behind the shoulders.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"model": str(donor.pk), "location": str(donor.pk)},
                    "keepTattoos": True,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        content = chat.call_args_list[0].args[2]
        self.assertEqual(len([item for item in content if item["type"] == "image_url"]), 2)
        donor_focus = next(
            item["text"]
            for item in content
            if item["type"] == "text" and item["text"].startswith("Image 2:")
        )
        self.assertIn("Location reference", donor_focus)
        self.assertIn("visible tattoos", donor_focus)
        self.assertIn("anatomical placement", donor_focus)

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
