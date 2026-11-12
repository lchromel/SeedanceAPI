"""Hair donors define pigmentation as well as styling throughout replacement edits."""

import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import ValidationError

from . import enhancer, prompt_format, replacements, test_replacements
from .models import ImageGeneration
from .test_prompt_format import brief


class HairColorControlsTests(SimpleTestCase):
    def data(self):
        return {
            "original": "original",
            "replacements": {"model": "model", "hairstyle": "hair"},
            "extras": [],
            "locationDescriptionOnly": False,
            "keepTattoos": False,
            "identity": {"eyeColor": "blue", "details": "Freckles."},
        }

    def test_control_is_idempotent_and_rebinds_or_removes_old_hair_sources(self):
        for draft in ("Edit Image 1, preserving the face.", brief()):
            with self.subTest(structured=bool(prompt_format.parse(draft))):
                data = self.data()
                first = replacements.apply_controls(draft, data)
                self.assertEqual(first.count(replacements.HAIR_COLOR_POLICIES["Image 3"]), 1)
                self.assertEqual(replacements.apply_controls(first, data), first)
                data["replacements"].pop("model")
                changed = replacements.apply_controls(first, data)
                self.assertNotIn(replacements.HAIR_COLOR_POLICIES["Image 3"], changed)
                self.assertEqual(changed.count(replacements.HAIR_COLOR_POLICIES["Image 2"]), 1)
                self.assertEqual(replacements.apply_controls(changed, data), changed)
                data["replacements"].pop("hairstyle")
                removed = replacements.apply_controls(changed, data)
                self.assertNotIn("the hairstyle reference", removed)
                self.assertNotIn(replacements.HAIR_COLOR_MARKER, removed)
                if prompt_format.parse(draft):
                    self.assertEqual(len(prompt_format.parse(changed)), 8)
                    self.assertEqual(len(prompt_format.parse(removed)), 8)

    def test_hair_source_uses_attached_order_with_shared_roles_and_text_only_location(self):
        data = self.data()
        data["replacements"] = {"model": "hair", "hairstyle": "hair", "location": "room"}
        data["extras"] = [{"asset": "hair", "label": "Hair"}]
        data["locationDescriptionOnly"] = True
        self.assertEqual(
            replacements.hair_color_policy("original", data),
            replacements.HAIR_COLOR_POLICIES["Image 2"],
        )

    def test_hair_control_respects_provider_length_limit(self):
        data = self.data()
        # Fits with the tattoo setting alone, but not with the required hair source.
        draft = "x" * (6000 - len(replacements.TATTOO_INSTRUCTIONS[False]) - 2)
        with self.assertRaises(ValidationError):
            replacements.apply_controls(draft, data)


@override_settings(
    DEBUG=True,
    GENERATION_PROVIDER="mock",
    CREDITS_ENABLED=False,
    S3_BUCKET="",
    SAVE_IMAGES_TO_S3=True,
)
class HairColorAPITests(TestCase):
    setUp = test_replacements.ReplacementTests.setUp
    upload = test_replacements.ReplacementTests.upload
    data = test_replacements.ReplacementTests.data
    generate = test_replacements.ReplacementTests.generate

    def test_hair_policy_reaches_vision_writer_and_saved_generation_with_text_only_location(self):
        original, model, hair, room = (
            self.upload("blue-haired-original"),
            self.upload("black-haired-model"),
            self.upload("blonde-hair-reference"),
            self.upload("room"),
        )
        data = self.data([original], {"model": model, "hairstyle": hair, "location": room})
        data["locationDescriptionOnly"] = True
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                json.dumps(
                    {
                        "images": [
                            {"image": 1, "description": "Person with blue hair on one bed."},
                            {"image": 2, "description": "Model with black hair."},
                            {
                                "image": 3,
                                "description": "Blonde waves with dark roots and golden highlights.",
                            },
                            {"image": 4, "description": "Restyle the occupied bed with cream linens."},
                        ]
                    }
                ),
                "Edit Image 1, using the face from Image 2 and waves from Image 3 on the cream bed.",
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": data["replacements"],
                    "instructions": data["instructions"],
                    "notes": data["originals"][0]["notes"],
                    "ratio": data["ratio"],
                    "resolution": data["resolution"],
                    "locationDescriptionOnly": True,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        context = json.loads(chat.call_args_list[1].args[2])
        policy = replacements.HAIR_COLOR_POLICIES["Image 3"]
        self.assertEqual(context["hairColorPolicy"], policy)
        self.assertEqual(context["replacementPlan"]["hairstyle"]["aspect"], "the hairstyle and hair color")
        self.assertIn("Blonde waves", context["referenceObservations"][2]["description"])
        vision_text = json.dumps(chat.call_args_list[0].args[2])
        self.assertIn("hair base hue", vision_text)
        self.assertIn("pigmentation", vision_text)
        self.assertEqual(response.data["prompt"].count(policy), 1)
        data["originals"][0].update(
            {key: response.data[key] for key in ("prompt", "locationDescription", "locationPreparation")}
        )
        result = self.generate(data)
        job = ImageGeneration.objects.get(pk=result.data[0]["id"])
        self.assertEqual(job.snapshot["prompt"], response.data["prompt"])
        self.assertEqual(job.snapshot["references"], [str(a.pk) for a in (original, model, hair)])

    def test_manual_prompt_also_receives_hair_color_source_before_generation(self):
        original, hair = self.upload("original"), self.upload("hair")
        data = self.data([original], {"hairstyle": hair})
        with patch.object(enhancer, "chat") as chat:
            result = self.generate(data)
        chat.assert_not_called()
        job = ImageGeneration.objects.get(pk=result.data[0]["id"])
        self.assertIn(replacements.HAIR_COLOR_POLICIES["Image 2"], job.snapshot["prompt"])
