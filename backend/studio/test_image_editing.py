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

    def location_batch(self):
        original, model, location, extra = [
            self.upload(name) for name in ("original", "model", "courtyard", "detail")
        ]
        data = self.data([original], {"model": model, "location": location})
        data.update(
            locationDescriptionOnly=True,
            extras=[{"asset": str(extra.pk), "label": "Necklace only"}],
        )
        description = (
            "An ochre courtyard with a blue door in a recessed opening, rough lime plaster walls, "
            "pale stone paving and a shaded arcade receding to the right. Soft daylight enters "
            "from the left, with low contrast shadows under the arcade. The far wall has small "
            "areas of worn plaster, while the paving joints form converging depth lines."
        )
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                json.dumps(
                    {
                        "images": [
                            {
                                "image": 1,
                                "description": "Tight eye-level portrait with shoulders cropped.",
                            },
                            {"image": 2, "description": "Identity source with brown eyes."},
                            {"image": 4, "description": "A silver necklace."},
                        ]
                    }
                ),
                description,
                "Edit Image 1 using the identity from Image 2 and the necklace from Image 3. Behind the cropped shoulders place a softly focused ochre plaster wall and the edge of a recessed blue door, lit by soft daylight from the left.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    **{
                        key: value
                        for key, value in data.items()
                        if key not in ("originals", "requestKey")
                    },
                    "original": str(original.pk),
                    "notes": data["originals"][0]["notes"],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        data["originals"][0].update(
            {
                key: response.data[key]
                for key in ("prompt", "locationDescription", "locationPreparation")
            }
        )
        return (original, model, location, extra), data, chat, description

    def test_description_only_location_is_observed_but_never_sent_to_seedream(self):
        from unittest.mock import Mock

        sources, data, chat, description = self.location_batch()
        original, model, location, extra = sources
        self.assertEqual(
            len([item for item in chat.call_args_list[0].args[2] if item["type"] == "image_url"]), 4
        )
        recovery = chat.call_args_list[1].args[2]
        self.assertIn("Original photograph, Image 1", recovery[0]["text"])
        self.assertIn("This is Image 3", recovery[2]["text"])
        self.assertIn("250–300 words", recovery[2]["text"])
        self.assertEqual(len([item for item in recovery if item["type"] == "image_url"]), 2)
        context = json.loads(chat.call_args_list[2].args[2])
        self.assertEqual(context["referenceLabels"], ["Image 1", "Image 2", "Image 3"])
        self.assertEqual(context["replacementPlan"]["location"]["source"], "text description")
        self.assertEqual(
            context["extraReferences"], [{"source": "Image 3", "role": "Necklace only"}]
        )
        self.assertEqual(context["locationDescription"], description)
        self.assertNotIn(
            description, [item["description"] for item in context["referenceObservations"]]
        )
        self.assertEqual(context["referenceObservations"][2]["description"], "A silver necklace.")
        result = self.generate(data)
        job = ImageGeneration.objects.get(pk=result.data[0]["id"])
        self.assertEqual(
            job.snapshot["references"], [str(original.pk), str(model.pk), str(extra.pk)]
        )
        self.assertNotIn(location.key, job.snapshot["reference_keys"])
        self.assertEqual(job.snapshot["location_description"], description)
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"url": "https://provider.example/result.jpg"}]}
        with (
            patch.object(image_tasks.requests, "post", return_value=response) as post,
            patch.object(
                storage,
                "url",
                side_effect=lambda key, **kw: "https://private.example/" + key,
            ),
        ):
            image_tasks.submit(job)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["image"],
            ["https://private.example/" + asset.key for asset in (original, model, extra)],
        )
        self.assertIn("ochre plaster wall", payload["prompt"])
        self.assertNotIn("Image 4", payload["prompt"])

    def test_description_only_requires_current_individual_preparation(self):
        import copy

        sources, data, _, _ = self.location_batch()
        mutations = [
            lambda item: item["originals"][0].update(locationPreparation=""),
            lambda item: item["originals"][0].update(locationDescription="Invented setting"),
            lambda item: item.update(instructions="Move to a different viewpoint"),
            lambda item: item["originals"][0].update(notes="New crop"),
            lambda item: item["originals"][0].update(asset=str(sources[1].pk)),
            lambda item: item["originals"][0].update(
                prompt="Edit Image 1 using Image 2, Image 3 and the old location Image 4."
            ),
        ]
        with patch.object(image_tasks, "submit") as submit:
            for mutate in mutations:
                altered = copy.deepcopy(data)
                mutate(altered)
                response = self.client.post(
                    "/api/images/replacements/generate", altered, format="json"
                )
                self.assertEqual(response.status_code, 400, response.data)
        submit.assert_not_called()
        self.assertFalse(ImageGeneration.objects.exists())

    def test_description_only_rejects_a_location_reused_in_another_role(self):
        original, donor = self.upload("original"), self.upload("donor")
        for roles in (
            {"location": str(original.pk)},
            {"location": str(donor.pk), "model": str(donor.pk)},
            {},
        ):
            with patch.object(enhancer, "chat") as chat:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        "replacements": roles,
                        "locationDescriptionOnly": True,
                    },
                    format="json",
                )
            self.assertEqual(response.status_code, 400)
            chat.assert_not_called()

    def test_hotel_bed_transfer_keeps_the_occupied_bed_as_the_same_support(self):
        original = self.upload("lying-on-bed")
        location = self.upload("hotel-room", (320, 180))
        data = self.data([original], {"location": location})
        data["locationDescriptionOnly"] = True
        transfer = (
            "Original support: one visible bed under a reclining person, mattress angled from lower left "
            "to upper right; torso and legs contact the mattress. Same-object match: the hotel's bed "
            "is this occupied bed. Target staging: one continuous mattress beneath the same pose, with "
            "headboard at the existing head end. Appearance: cream linens, a walnut headboard and warm "
            "wall finishes from the hotel; show only room surfaces fitting the original crop."
        )
        full_prompt = (
            "Edit Image 1, retaining the reclining pose and camera. Restyle the single occupied bed "
            "with cream hotel linens and a walnut headboard at its existing head end. Its mattress "
            "supports the torso and legs in the original perspective. Place warm room wall finishes "
            "around this one bed within the original crop."
        )
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                json.dumps(
                    {
                        "images": [
                            {
                                "image": 1,
                                "description": "Person reclining on one bed, body contacting its angled mattress.",
                            },
                            {"image": 2, "description": transfer},
                        ]
                    }
                ),
                full_prompt,
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": data["replacements"],
                    "locationDescriptionOnly": True,
                    "instructions": data["instructions"],
                    "notes": data["originals"][0]["notes"],
                    "ratio": data["ratio"],
                    "resolution": data["resolution"],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(chat.call_count, 2)
        self.assertIn("JOINT transfer plan", chat.call_args_list[0].args[2][0]["text"])
        self.assertIn("Same-object match", chat.call_args_list[0].args[2][0]["text"])
        context = json.loads(chat.call_args_list[1].args[2])
        self.assertEqual(context["locationDescription"], transfer)
        self.assertIn("one bed", context["referenceObservations"][0]["description"])
        data["originals"][0].update(
            {
                key: response.data[key]
                for key in ("prompt", "locationDescription", "locationPreparation")
            }
        )
        job = ImageGeneration.objects.get(pk=self.generate(data).data[0]["id"])
        self.assertEqual(job.snapshot["references"], [str(original.pk)])
        self.assertEqual(job.snapshot["location_geometry_version"], 2)
        self.assertNotIn(replacements.LOCATION_GEOMETRY_MARKER, job.snapshot["prompt"])
        self.assertIn("single occupied bed", job.snapshot["prompt"])

    def test_preparation_from_before_the_support_geometry_fix_requires_refresh(self):
        import hashlib
        from django.core import signing

        sources, data, _, description = self.location_batch()
        serializer = replacements.ReplacementPrompt(
            data={
                "original": str(sources[0].pk),
                "replacements": data["replacements"],
                "instructions": data["instructions"],
                "notes": data["originals"][0]["notes"],
                "extras": data["extras"],
                "locationDescriptionOnly": True,
                "ratio": data["ratio"],
                "resolution": data["resolution"],
            },
            context={"user": self.user},
        )
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        old_context = {
            key: validated[key]
            for key in (
                "replacements",
                "extras",
                "instructions",
                "ratio",
                "resolution",
                "keepTattoos",
                "identity",
            )
        }
        old_context.update(original=str(sources[0].pk), notes=validated["notes"])
        data["originals"][0]["locationPreparation"] = signing.dumps(
            {
                "context": hashlib.sha256(
                    json.dumps(old_context, sort_keys=True).encode()
                ).hexdigest(),
                "description": hashlib.sha256(description.encode()).hexdigest(),
            },
            salt="studio.location-description",
        )
        response = self.client.post("/api/images/replacements/generate", data, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageGeneration.objects.exists())

    def test_unchecked_location_continues_to_be_attached_as_an_image(self):
        original, location = self.upload("original"), self.upload("location")
        response = self.generate(self.data([original], {"location": location}))
        job = ImageGeneration.objects.get(pk=response.data[0]["id"])
        self.assertFalse(job.snapshot["location_description_only"])
        self.assertEqual(job.snapshot["references"], [str(original.pk), str(location.pk)])

    @override_settings(
        SAVE_IMAGES_TO_S3=False, IMAGE_TEMP_REDIS_URL="redis://test-only", S3_BUCKET="unused"
    )
    def test_description_only_temporary_location_is_cleaned_up_and_not_needed_for_regeneration(
        self,
    ):
        cache = ExpiringRedis()
        with patch.object(storage, "temporary_client", return_value=cache):
            sources, data, _, description = self.location_batch()
            response = self.generate(data)
            job = ImageGeneration.objects.get(pk=response.data[0]["id"])
            with self.captureOnCommitCallbacks(execute=True):
                image_tasks.process_image(job.pk)
            self.assertFalse(Asset.objects.exists())
            with self.assertRaises(FileNotFoundError):
                storage.temporary_bytes(sources[2].key)
            fresh = [
                self.upload(name) for name in ("fresh-original", "fresh-model", "fresh-detail")
            ]
            request = {
                "requestKey": str(uuid.uuid4()),
                "references": [str(asset.pk) for asset in fresh],
            }
            with patch.object(enhancer, "chat") as chat:
                repeated = self.client.post(
                    f"/api/images/{job.pk}/regenerate", request, format="json"
                )
            self.assertEqual(repeated.status_code, 201, repeated.data)
            retry = ImageGeneration.objects.get(pk=repeated.data["id"])
            self.assertTrue(retry.snapshot["location_description_only"])
            self.assertEqual(retry.snapshot["location_geometry_version"], 2)
            self.assertEqual(retry.snapshot["location_description"], description)
            self.assertEqual(len(retry.snapshot["reference_keys"]), 3)
            chat.assert_not_called()

    def prepare_shared_location(self, location):
        identity = "Hotel room with cream linen bedding, a rectangular walnut headboard, warm beige plaster walls and matte brass reading lights. Keep the same observed colors and materials across views."
        with patch.object(
            enhancer,
            "chat",
            return_value=json.dumps(
                {
                    "images": [
                        {"image": 1, "description": identity},
                    ]
                }
            ),
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/location-profile",
                {
                    "asset": str(location.pk),
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(chat.call_count, 1)
        self.assertIn("fixed visual identity", chat.call_args.args[2][1]["text"])
        return response.data, identity

    def test_shared_location_identity_is_reused_in_distinct_individual_prompts_and_jobs(self):
        first, second, location = (
            self.upload("reclining"),
            self.upload("seated"),
            self.upload("hotel"),
        )
        profile, identity = self.prepare_shared_location(location)
        data = self.data([first, second], {"location": location})
        data.update(locationDescriptionOnly=True, locationProfile=profile)
        descriptions = []
        for i, original in enumerate((first, second)):
            observation = (
                "Reclining person on one bed, low oblique camera."
                if i == 0
                else "Person sitting at the edge of one bed, eye-level camera."
            )
            transfer = (
                observation
                + " Restyle that same bed with cream linens and a walnut headboard; beige plaster and brass lights remain the same hotel identity."
            )
            descriptions.append(transfer)
            with patch.object(
                enhancer,
                "chat",
                side_effect=[
                    json.dumps(
                        {
                            "images": [
                                {"image": 1, "description": observation},
                                {"image": 2, "description": transfer},
                            ]
                        }
                    ),
                    "Edit Image 1. " + transfer,
                ],
            ) as chat:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        "replacements": data["replacements"],
                        "instructions": data["instructions"],
                        "notes": data["originals"][i]["notes"],
                        "ratio": data["ratio"],
                        "resolution": data["resolution"],
                        "locationDescriptionOnly": True,
                        "locationProfile": profile,
                    },
                    format="json",
                )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(chat.call_count, 2)
            context = json.loads(chat.call_args_list[1].args[2])
            self.assertEqual(context["sharedLocationIdentity"], identity)
            self.assertIn(identity, chat.call_args_list[0].args[2][3]["text"])
            self.assertEqual(context["referenceObservations"][0]["description"], observation)
            data["originals"][i].update(
                {
                    key: response.data[key]
                    for key in ("prompt", "locationDescription", "locationPreparation")
                }
            )
        result = self.generate(data)
        for i, item in enumerate(result.data):
            job = ImageGeneration.objects.get(pk=item["id"])
            self.assertEqual(job.snapshot["location_profile"], profile)
            self.assertEqual(job.snapshot["location_description"], descriptions[i])
            self.assertEqual(job.snapshot["references"], [str((first, second)[i].pk)])
        self.assertNotEqual(descriptions[0], descriptions[1])

    def test_shared_location_profile_cannot_be_changed_or_applied_to_another_image_or_owner(self):
        original, location, other_location = (
            self.upload("original"),
            self.upload("hotel"),
            self.upload("other-hotel"),
        )
        profile, _ = self.prepare_shared_location(location)
        cases = [
            ({**profile, "description": "A different invented room."}, location),
            (profile, other_location),
            ({"asset": str(location.pk)}, location),
        ]
        with patch.object(enhancer, "chat") as chat:
            for supplied, selected in cases:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        "replacements": {"location": str(selected.pk)},
                        "locationProfile": supplied,
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)
            self.client.force_authenticate(self.other)
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"location": str(location.pk)},
                    "locationProfile": profile,
                },
                format="json",
            )
            self.assertEqual(response.status_code, 400)
        chat.assert_not_called()
        self.assertFalse(ImageGeneration.objects.exists())

    def test_shared_location_endpoint_respects_ownership_and_analysis_failure(self):
        location = self.upload("hotel")
        location.owner = self.other
        location.save()
        with patch.object(enhancer, "chat") as chat:
            response = self.client.post(
                "/api/images/replacements/location-profile",
                {"asset": str(location.pk)},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        chat.assert_not_called()
        location.owner = self.user
        location.save()
        with patch.object(
            enhancer, "chat", side_effect=enhancer.EnhancementError("Vision unavailable")
        ):
            response = self.client.post(
                "/api/images/replacements/location-profile",
                {"asset": str(location.pk)},
                format="json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertFalse(ImageGeneration.objects.exists())

    def test_location_preparation_is_bound_to_the_shared_identity_used_for_that_photo(self):
        sources, data, _, _ = self.location_batch()
        profile, _ = self.prepare_shared_location(sources[2])
        # The existing individual prompt was prepared without this shared identity.
        data["locationProfile"] = profile
        response = self.client.post("/api/images/replacements/generate", data, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageGeneration.objects.exists())

    def test_known_replacement_roles_fill_omitted_labels_without_another_ai_request(self):
        original, clothing = self.upload("original"), self.upload("outfit")
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                json.dumps(
                    {
                        "images": [
                            {"image": 1, "description": "Person standing in a room."},
                            {"image": 2, "description": "A fitted blue jacket."},
                        ]
                    }
                ),
                "Keep the standing pose and room. Dress the person in the fitted blue jacket with natural fabric folds.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"clothing": str(clothing.pk)},
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(chat.call_count, 2)
        self.assertIn("Use Image 1 for the original photograph to edit.", response.data["prompt"])
        self.assertIn("Use Image 2 for the clothing and accessories.", response.data["prompt"])
        self.assertIn("fitted blue jacket with natural fabric folds", response.data["prompt"])
        self.assertFalse(ImageGeneration.objects.exists())

    def test_analysis_labels_are_rebound_after_location_exclusion_before_writing(self):
        original, location, extra = (
            self.upload("original"),
            self.upload("location"),
            self.upload("fabric"),
        )
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                json.dumps(
                    {
                        "images": [
                            {"image": 1, "description": "Image 1 shows a seated person."},
                            {
                                "image": 2,
                                "description": "Match the bed in Image 2 to the occupied bed in Image 1.",
                            },
                            {"image": 3, "description": "Image 3 supplies gray fabric."},
                        ]
                    }
                ),
                "Edit Image 1 with the gray fabric from Image 2, keeping the seated pose and the same occupied bed.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"location": str(location.pk)},
                    "extras": [{"asset": str(extra.pk), "label": "Fabric only"}],
                    "locationDescriptionOnly": True,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        context = json.loads(chat.call_args_list[1].args[2])
        self.assertEqual(
            context["referenceObservations"][1]["description"], "Image 2 supplies gray fabric."
        )
        self.assertEqual(
            context["locationDescription"],
            "Match the bed in the described location to the occupied bed in Image 1.",
        )
        self.assertNotIn("Image 3", json.dumps(context))
        self.assertNotIn("Image 3", response.data["prompt"])

    def test_unknown_replacement_labels_still_fail_and_diagnostics_do_not_log_prompt_content(self):
        original, clothing = self.upload("original"), self.upload("clothing")
        private_phrase = "PRIVATE SCENE DETAILS MUST NOT APPEAR IN LOGS"
        with (
            self.assertLogs("studio.images", level="WARNING") as logs,
            patch.object(
                enhancer,
                "chat",
                side_effect=[
                    '{"images":[{"image":1,"description":"Person"},{"image":2,"description":"Coat"}]}',
                    "Use Image 9 for the coat. " + private_phrase,
                    "Use Image 9 for the coat. " + private_phrase,
                ],
            ) as chat,
        ):
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"clothing": str(clothing.pk)},
                },
                format="json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(chat.call_count, 3)
        self.assertIn("unexpected: Image 9", response.data["detail"])
        self.assertIn("Missing: Image 1, Image 2", response.data["detail"])
        self.assertIn("Repair only the reference citations", chat.call_args_list[2].args[1])
        self.assertNotIn(private_phrase, "\n".join(logs.output))
        self.assertFalse(ImageGeneration.objects.exists())

    def test_shared_location_description_drops_its_analysis_image_label(self):
        location = self.upload("hotel")
        with patch.object(
            enhancer,
            "chat",
            return_value='{"images":[{"image":1,"description":"Image 1 has cream linens and a walnut headboard."}]}',
        ):
            response = self.client.post(
                "/api/images/replacements/location-profile",
                {"asset": str(location.pk)},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["description"], "this location has cream linens and a walnut headboard."
        )
