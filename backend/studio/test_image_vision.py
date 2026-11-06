import base64
import io
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from . import enhancer, image_vision, storage
from .models import Asset, ImageGeneration, Ledger


class VisionFormatTests(SimpleTestCase):
    def test_explicit_labels_and_reordered_arrays_keep_their_original_image_numbers(self):
        for first, second in (("Image 2", "1"), ("@image2", "Image #1"), (2, 1), ("2", "1")):
            with self.subTest(first=first, second=second):
                text = json.dumps(
                    {
                        "images": [
                            {"image": first, "description": "Blue"},
                            {"image": second, "description": "Red"},
                        ]
                    }
                )
                self.assertEqual(image_vision.observations(text, 2), {1: "Red", 2: "Blue"})

    def test_fenced_mappings_and_explicit_label_aliases_are_normalized(self):
        for text in (
            '```JSON\n{"images":{"Image 2":"Blue","Image 1":"Red"}}\n```',
            '[{"image_number":"2","description":"Blue"},{"label":"Image 1","observation":"Red"}]',
        ):
            self.assertEqual(image_vision.observations(text, 2), {1: "Red", 2: "Blue"})

    def test_missing_duplicate_and_invalid_descriptions_require_real_reanalysis(self):
        for invalid in ("", " " * 10, "x" * 2401, {"subject": "invented structure"}):
            text = json.dumps(
                {
                    "images": [
                        {"image": 1, "description": invalid},
                        {"image": 2, "description": "Blue"},
                    ]
                }
            )
            self.assertEqual(image_vision.observations(text, 2), {2: "Blue"})
        duplicate = '{"images":[{"image":1,"description":"Red"},{"image":1,"description":"Green"},{"image":2,"description":"Blue"}]}'
        self.assertEqual(image_vision.observations(duplicate, 2), {2: "Blue"})
        duplicate_empty = '{"images":[{"image":1,"description":""},{"image":1,"description":"Green"},{"image":2,"description":"Blue"}]}'
        self.assertEqual(image_vision.observations(duplicate_empty, 2), {2: "Blue"})
        self.assertEqual(
            image_vision.observations(
                '{"images":{"Image 1":"Red","Image 1":"Green","Image 2":"Blue"}}', 2
            ),
            {},
        )
        self.assertEqual(image_vision.observations('{"images":["Red","Blue"]}', 2), {})

    def test_zero_based_or_conflicting_labels_are_never_silently_reassigned(self):
        self.assertEqual(
            image_vision.observations(
                '{"images":[{"image":0,"description":"Red"},{"image":1,"description":"Blue"}]}', 2
            ),
            {},
        )
        self.assertEqual(
            image_vision.observations('{"images":[{"image":1,"index":2,"description":"Red"}]}', 2),
            {},
        )
        self.assertEqual(
            image_vision.observations('{"images":[{"image":true,"description":"Red"}]}', 1), {}
        )

    def test_expired_budget_stops_before_another_inference_request(self):
        with self.assertRaises(enhancer.EnhancementError):
            image_vision.timeout(time.monotonic() - 1)


@override_settings(DEBUG=True, S3_BUCKET="", CREDITS_ENABLED=False)
class VisionRecoveryTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        configured = override_settings(MEDIA_ROOT=Path(directory.name))
        configured.enable()
        self.addCleanup(configured.disable)
        self.user = get_user_model().objects.create_user("vision-owner")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.assets = []
        for i, color in enumerate(("red", "blue"), 1):
            key = f"{self.user.pk}/assets/{i}.jpg"
            buffer = io.BytesIO()
            Image.new("RGB", (160, 120), color).save(buffer, "JPEG")
            buffer.seek(0)
            storage.put(key, buffer, "image/jpeg")
            self.assets.append(
                Asset.objects.create(
                    owner=self.user, kind="image", key=key, mime="image/jpeg", name=f"Source {i}"
                )
            )
        self.data = {
            "prompt": "Use the object from Image 1 with the color palette from Image 2.",
            "references": [str(asset.pk) for asset in self.assets],
        }

    def test_number_strings_and_reversed_order_need_no_additional_vision_calls(self):
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":"Image 2","description":"Blue"},{"image":"1","description":"Red"}]}',
                self.data["prompt"],
            ],
        ) as chat:
            response = self.client.post("/api/images/enhance", self.data, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(chat.call_count, 2)
        context = json.loads(chat.call_args_list[1].args[2])
        self.assertEqual(
            context["referenceObservations"],
            [{"image": 1, "description": "Red"}, {"image": 2, "description": "Blue"}],
        )

    def test_partial_analysis_recovers_only_the_missing_actual_image(self):
        calls = []

        def chat(model, system, content, **kwargs):
            calls.append((model, content))
            if model == settings.DEEPSEEK_MODEL:
                context = json.loads(content)
                self.assertEqual(
                    context["referenceObservations"],
                    [{"image": 1, "description": "Red"}, {"image": 2, "description": "Blue"}],
                )
                return self.data["prompt"]
            pictures = [item for item in content if item["type"] == "image_url"]
            if len(pictures) == 2:
                return '{"images":[{"image":2,"description":"Blue"}]}'
            self.assertEqual(len(pictures), 1)
            self.assertIn("Image 1", content[0]["text"])
            with Image.open(
                io.BytesIO(base64.b64decode(pictures[0]["image_url"]["url"].split(",", 1)[1]))
            ) as actual:
                red, green, blue = actual.getpixel((0, 0))
                self.assertGreater(red, green + 100)
                self.assertGreater(red, blue + 100)
            return '```json\n{"images":[{"image":"first","description":"Red"}]}\n```'

        with patch.object(enhancer, "chat", side_effect=chat):
            response = self.client.post("/api/images/enhance", self.data, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(calls), 3)
        self.assertFalse(ImageGeneration.objects.exists())
        self.assertFalse(Ledger.objects.exists())

    def test_malformed_aggregate_response_recovers_each_source_in_isolation(self):
        seen = []

        def chat(model, system, content, **kwargs):
            if model == settings.DEEPSEEK_MODEL:
                self.assertEqual(
                    json.loads(content)["referenceObservations"],
                    [{"image": 1, "description": "Red"}, {"image": 2, "description": "Blue"}],
                )
                return self.data["prompt"]
            pictures = [item for item in content if item["type"] == "image_url"]
            if len(pictures) == 2:
                return "incomplete JSON"
            self.assertEqual(len(pictures), 1)
            with Image.open(
                io.BytesIO(base64.b64decode(pictures[0]["image_url"]["url"].split(",", 1)[1]))
            ) as actual:
                red, _, blue = actual.getpixel((0, 0))
            color = "Red" if red > blue else "Blue"
            seen.append(color)
            return color

        with patch.object(enhancer, "chat", side_effect=chat):
            response = self.client.post("/api/images/enhance", self.data, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertCountEqual(seen, ["Red", "Blue"])

    def test_unrecoverable_analysis_never_reaches_the_writer_or_launches_generation(self):
        def chat(model, system, content, **kwargs):
            self.assertEqual(model, settings.REFERENCE_VISION_MODEL)
            pictures = [item for item in content if item["type"] == "image_url"]
            if len(pictures) == 2:
                return '{"images":[]}'
            raise enhancer.EnhancementError("Vision unavailable")

        with patch.object(enhancer, "chat", side_effect=chat):
            response = self.client.post("/api/images/enhance", self.data, format="json")
        self.assertEqual(response.status_code, 502)
        self.assertIn("original prompt is unchanged", response.data["detail"])
        self.assertEqual(Asset.objects.count(), 2)
        self.assertFalse(ImageGeneration.objects.exists())
        self.assertFalse(Ledger.objects.exists())

    def test_location_recovery_sees_original_and_donor_at_detail_resolution(self):
        for asset, color in zip(self.assets, ("red", "blue")):
            buffer = io.BytesIO()
            Image.new("RGB", (1600, 1000), color).save(buffer, "JPEG")
            buffer.seek(0)
            storage.put(asset.key, buffer, "image/jpeg")
        transfer = (
            "Original support: one bed under the person. Same-object match: the hotel bed is "
            "that existing bed. Target staging: retain its mattress perspective and body contact. "
            "Appearance: cream linens and a walnut headboard on this single bed."
        )
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"Person reclining on one bed."}]}',
                transfer,
            ],
        ) as chat:
            result = image_vision.analyze(
                self.assets, time.monotonic() + 100, detailed_references=[2], scene_locations=[2]
            )
        self.assertEqual(chat.call_count, 2)
        self.assertIn("JOINT transfer plan", chat.call_args_list[0].args[2][0]["text"])
        recovery = chat.call_args_list[1].args[2]
        self.assertIn("Original photograph, Image 1", recovery[0]["text"])
        self.assertIn("This is Image 2", recovery[2]["text"])
        self.assertIn("Same-object match", recovery[2]["text"])
        pictures = [item for item in recovery if item["type"] == "image_url"]
        self.assertEqual(len(pictures), 2)
        for i, picture in enumerate(pictures):
            with Image.open(
                io.BytesIO(base64.b64decode(picture["image_url"]["url"].split(",", 1)[1]))
            ) as actual:
                self.assertEqual(actual.size, (1280, 800))
                red, _, blue = actual.getpixel((0, 0))
                self.assertGreater(red if i == 0 else blue, (blue if i == 0 else red) + 100)
        self.assertEqual(result[1]["description"], transfer)
