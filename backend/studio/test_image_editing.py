"""Editing modes and explicit fresh image attempts, using local mock generation."""

import json
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from . import enhancer, image_tasks, replacements, storage, test_replacements
from .models import Asset, ImageGeneration, Ledger
from .test_image_retention import ExpiringRedis


@override_settings(
    DEBUG=True,
    GENERATION_PROVIDER="mock",
    CREDITS_ENABLED=False,
    S3_BUCKET="",
    SAVE_IMAGES_TO_S3=True,
)
class EditingModesTests(TestCase):
    # Reuse upload/client helpers without inheriting the existing test cases.
    setUp = test_replacements.ReplacementTests.setUp
    upload = test_replacements.ReplacementTests.upload
    data = test_replacements.ReplacementTests.data
    generate = test_replacements.ReplacementTests.generate

    def test_makeup_context_preserves_original_identity_and_has_separate_hair_role(self):
        original, makeup, hair = self.upload("portrait"), self.upload("makeup"), self.upload("hair")
        data = {
            "workflow": "makeup",
            "original": str(original.pk),
            "replacements": {"makeup": str(makeup.pk), "hairstyle": str(hair.pk)},
            "identity": {"eyeColor": "brown", "details": "A small mole below the left eye."},
        }
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                json.dumps(
                    {
                        "images": [
                            {"image": 1, "description": "Portrait of a person with brown eyes."},
                            {
                                "image": 2,
                                "description": "Green-eyed donor with plum lipstick and soft brown eye shadow.",
                            },
                            {"image": 3, "description": "Another person with a dark braided updo."},
                        ]
                    }
                ),
                "Edit Image 1, keeping its face and brown irises. Apply the plum lipstick and brown eye shadow from Image 2, with the braided updo from Image 3.",
            ],
        ) as chat:
            response = self.client.post("/api/images/replacements/prompt", data, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        context = json.loads(chat.call_args_list[1].args[2])
        self.assertEqual(context["workflow"], "makeup")
        self.assertEqual(context["identityLock"], data["identity"])
        self.assertEqual(context["replacementPlan"]["model"]["source"], "original")
        self.assertEqual(context["replacementPlan"]["hairstyle"]["source"], "Image 3")
        self.assertIn("iris color consistently brown", response.data["prompt"])
        self.assertIn("facial identity", response.data["prompt"])
        self.assertIn("A small mole below the left eye.", response.data["prompt"])
        batch = self.data([original], {"makeup": makeup, "hairstyle": hair})
        batch.update(workflow="makeup", identity=data["identity"])
        batch["originals"][0]["prompt"] = response.data["prompt"]
        created = self.generate(batch)
        job = ImageGeneration.objects.get(pk=created.data[0]["id"])
        self.assertEqual(job.snapshot["mode"], "makeup")
        self.assertEqual(job.snapshot["identity_lock"], data["identity"])
        self.assertEqual(job.snapshot["prompt"], response.data["prompt"])
        self.assertEqual(job.snapshot["prompt"].count(replacements.IDENTITY_MARKER), 1)
        self.assertEqual(job.snapshot["prompt"].count(replacements.WORKFLOW_MARKER), 1)

    def test_wardrobe_garments_and_labeled_extras_keep_order_and_view_scope(self):
        original = self.upload("three-view-sheet", (480, 240))
        coat, shoes = self.upload("coat"), self.upload("shoes")
        batch = self.data([original], {"clothing": coat})
        batch.update(workflow="wardrobe", extras=[{"asset": str(shoes.pk), "label": "Shoes only"}])
        response = self.generate(batch)
        job = ImageGeneration.objects.get(pk=response.data[0]["id"])
        self.assertEqual(
            job.snapshot["references"], [str(original.pk), str(coat.pk), str(shoes.pk)]
        )
        self.assertEqual(job.snapshot["mode"], "wardrobe")
        self.assertIn("every existing view", job.snapshot["prompt"])
        self.assertIn("facial identity", job.snapshot["prompt"])
        self.assertEqual(job.snapshot["extra_references"], batch["extras"])
        self.assertEqual(job.snapshot["ratio"], "2:1")

    def test_extra_captions_are_assigned_to_exact_image_labels_in_writer_context(self):
        original, model, detail = (
            self.upload("original"),
            self.upload("model"),
            self.upload("necklace"),
        )
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                json.dumps(
                    {
                        "images": [
                            {"image": i, "description": description}
                            for i, description in enumerate(
                                [
                                    "Original seated portrait.",
                                    "Person with a short haircut.",
                                    "A silver necklace.",
                                ],
                                1,
                            )
                        ]
                    }
                ),
                "Edit Image 1 using the person from Image 2. Add only the silver necklace from Image 3.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"model": str(model.pk)},
                    "extras": [{"asset": str(detail.pk), "label": "Necklace only; keep clothing"}],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        context = json.loads(chat.call_args_list[1].args[2])
        self.assertEqual(
            context["extraReferences"],
            [{"source": "Image 3", "role": "Necklace only; keep clothing"}],
        )

    def test_invalid_focused_modes_and_foreign_or_excess_extra_refs_stop_before_ai(self):
        original, reference = self.upload("original"), self.upload("reference")
        foreign = self.upload("foreign")
        foreign.owner = self.other
        foreign.save()
        cases = [
            {"workflow": "makeup"},
            {"workflow": "wardrobe"},
            {"workflow": "makeup", "replacements": {"model": str(reference.pk)}},
            {"extras": [{"asset": str(foreign.pk), "label": "Jewelry"}]},
            {"extras": [{"asset": str(reference.pk), "label": "   "}]},
            {"identity": {"eyeColor": "invented-color"}},
        ]
        references = [self.upload(f"extra-{i}") for i in range(10)]
        cases.append(
            {
                "replacements": {"model": str(reference.pk)},
                "extras": [{"asset": str(asset.pk), "label": "Detail"} for asset in references[:9]],
            }
        )
        with patch.object(enhancer, "chat") as chat:
            for fields in cases:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        **fields,
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, 400, (fields, response.data))
        chat.assert_not_called()
        multi = self.data([original, reference], {"makeup": references[0]})
        multi["workflow"] = "makeup"
        response = self.client.post("/api/images/replacements/generate", multi, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageGeneration.objects.exists())

    def test_changed_identity_footer_replaces_old_values_without_duplication(self):
        previous = replacements.apply_controls(
            "Edit Image 1.",
            {
                "keepTattoos": False,
                "identity": {"eyeColor": "blue", "details": "Freckles."},
            },
        )
        current = replacements.apply_controls(
            previous,
            {
                "keepTattoos": True,
                "identity": {"eyeColor": "brown", "details": "A small mole."},
            },
        )
        self.assertNotIn("blue", current)
        self.assertNotIn("Freckles.", current)
        self.assertNotIn(replacements.TATTOO_INSTRUCTIONS[False], current)
        self.assertIn("consistently brown", current)
        self.assertEqual(current.count(replacements.TATTOO_INSTRUCTIONS[True]), 1)

    def test_regeneration_is_individual_idempotent_and_preserves_old_result(self):
        original, second = self.upload("one"), self.upload("two")
        created = self.generate(self.data([original, second]))
        source = ImageGeneration.objects.get(pk=created.data[0]["id"])
        with self.captureOnCommitCallbacks(execute=True):
            image_tasks.process_image(source.pk)
        source.refresh_from_db()
        request = {"requestKey": str(uuid.uuid4())}
        with patch.object(image_tasks, "submit") as submit:
            first = self.client.post(f"/api/images/{source.pk}/regenerate", request, format="json")
            again = self.client.post(f"/api/images/{source.pk}/regenerate", request, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(again.data["id"], first.data["id"])
        self.assertEqual(first.data["regeneratedFrom"], str(source.pk))
        self.assertEqual(ImageGeneration.objects.count(), 3)
        retry = ImageGeneration.objects.get(pk=first.data["id"])
        self.assertEqual(retry.snapshot["prompt"], source.snapshot["prompt"])
        self.assertEqual(retry.snapshot["references"], [str(original.pk)])
        source.refresh_from_db()
        self.assertEqual(source.state, "ready")
        self.assertTrue(source.output_key)
        submit.assert_not_called()
        self.assertFalse(Ledger.objects.exists())

    def test_regeneration_cannot_access_another_owner_or_resubmit_inflight_or_review(self):
        source = ImageGeneration.objects.get(
            pk=self.generate(self.data([self.upload("one")])).data[0]["id"]
        )
        for state in ("queued", "generating", "downloading", "review"):
            source.state = state
            source.save()
            response = self.client.post(
                f"/api/images/{source.pk}/regenerate",
                {"requestKey": str(uuid.uuid4())},
                format="json",
            )
            self.assertEqual(response.status_code, 400)
        source.owner = self.other
        source.save()
        response = self.client.post(
            f"/api/images/{source.pk}/regenerate", {"requestKey": str(uuid.uuid4())}, format="json"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ImageGeneration.objects.count(), 1)

    @override_settings(
        SAVE_IMAGES_TO_S3=False, IMAGE_TEMP_REDIS_URL="redis://test-only", S3_BUCKET="unused"
    )
    def test_temporary_regeneration_uses_fresh_uploads_without_retaining_old_references(self):
        cache = ExpiringRedis()
        with patch.object(storage, "temporary_client", return_value=cache):
            original, clothing = self.upload("person"), self.upload("coat")
            data = self.data([original], {"clothing": clothing})
            data["workflow"] = "wardrobe"
            source = ImageGeneration.objects.get(pk=self.generate(data).data[0]["id"])
            with self.captureOnCommitCallbacks(execute=True):
                image_tasks.process_image(source.pk)
            self.assertFalse(Asset.objects.exists())
            request = {"requestKey": str(uuid.uuid4())}
            expired = self.client.post(
                f"/api/images/{source.pk}/regenerate", request, format="json"
            )
            self.assertEqual(expired.status_code, 400)
            first, second = self.upload("fresh-person"), self.upload("fresh-coat")
            request["references"] = [str(first.pk), str(second.pk)]
            response = self.client.post(
                f"/api/images/{source.pk}/regenerate", request, format="json"
            )
            self.assertEqual(response.status_code, 201, response.data)
            retry = ImageGeneration.objects.get(pk=response.data["id"])
            self.assertEqual(retry.snapshot["references"], request["references"])
            self.assertEqual(retry.snapshot["replacement_roles"]["clothing"], str(second.pk))
            self.assertEqual(retry.snapshot["original_id"], str(first.pk))
            self.assertEqual(retry.snapshot["mode"], "wardrobe")
            with self.captureOnCommitCallbacks(execute=True):
                image_tasks.process_image(retry.pk)
            self.assertFalse(Asset.objects.exists())
            source.refresh_from_db()
            self.assertTrue(storage.temporary_bytes(source.output_key))
            repeated = self.client.post(
                f"/api/images/{source.pk}/regenerate", request, format="json"
            )
            self.assertEqual(repeated.data["id"], str(retry.pk))
