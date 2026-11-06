import json
import time
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from . import enhancer, image_vision, test_replacements, vision_cache
from .models import ImageObservation


@override_settings(
    DEBUG=True,
    GENERATION_PROVIDER="mock",
    CREDITS_ENABLED=False,
    S3_BUCKET="",
    SAVE_IMAGES_TO_S3=True,
)
class VisionCacheTests(TestCase):
    setUp = test_replacements.ReplacementTests.setUp
    upload = test_replacements.ReplacementTests.upload

    def test_shared_reference_and_successful_original_are_not_reanalyzed_on_retry(self):
        first, second, donor = self.upload("first"), self.upload("second"), self.upload("model")
        for i, original in enumerate((first, second)):
            with patch.object(
                enhancer,
                "chat",
                side_effect=[
                    json.dumps(
                        {
                            "images": [
                                {"image": 1, "description": f"Original pose {i}."},
                                {"image": 2, "description": "Image 2 shows the same model."},
                            ]
                        }
                    ),
                    "Edit Image 1 using the identity from Image 2. Preserve pose and scene.",
                ],
            ) as chat:
                response = self.client.post(
                    "/api/images/replacements/prompt",
                    {
                        "original": str(original.pk),
                        "replacements": {"model": str(donor.pk)},
                    },
                    format="json",
                )
            self.assertEqual(response.status_code, 200, response.data)
            pictures = [
                item for item in chat.call_args_list[0].args[2] if item["type"] == "image_url"
            ]
            self.assertEqual(len(pictures), 2 if i == 0 else 1)
        with patch.object(
            enhancer, "chat", return_value="Edit Image 1 using the identity from Image 2."
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(second.pk),
                    "replacements": {"model": str(donor.pk)},
                    "notes": "Refine the light",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(chat.call_count, 1)
        context = json.loads(chat.call_args.args[2])
        self.assertEqual(
            context["referenceObservations"][1]["description"],
            "this reference shows the same model.",
        )
        self.assertEqual(ImageObservation.objects.exclude(description="").count(), 3)

    def test_partial_success_is_saved_and_retry_sends_only_the_failed_reference(self):
        assets = [self.upload(name) for name in ("one", "two", "three")]
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"First"},{"image":2,"description":"Second"}]}',
                enhancer.EnhancementError("Timeout", reason="timeout", retryable=True),
                enhancer.EnhancementError("Timeout", reason="timeout", retryable=True),
            ],
        ):
            with self.assertRaises(enhancer.EnhancementError) as error:
                image_vision.analyze(assets, time.monotonic() + 100, reuse=True)
        self.assertIn("timed out", str(error.exception))
        self.assertEqual(ImageObservation.objects.exclude(description="").count(), 2)
        with patch.object(
            enhancer, "chat", return_value='{"images":[{"image":3,"description":"Third"}]}'
        ) as chat:
            result = image_vision.analyze(assets, time.monotonic() + 100, reuse=True)
        self.assertEqual(chat.call_count, 1)
        content = chat.call_args.args[2]
        self.assertEqual(len([item for item in content if item["type"] == "image_url"]), 1)
        self.assertIn("Image 3:", content[1]["text"])
        self.assertEqual([item["description"] for item in result], ["First", "Second", "Third"])

    def test_lease_prevents_duplicate_work_and_deletion_removes_cached_observations(self):
        asset = self.upload("reference")
        key = vision_cache.key(asset, "clothing", False)
        value, first_claim = vision_cache.acquire(asset, key)
        self.assertIsNone(value)
        self.assertIsNotNone(first_claim)
        self.assertEqual(vision_cache.acquire(asset, key), (None, None))
        vision_cache.save(key, first_claim, "Blue coat")
        self.assertEqual(vision_cache.acquire(asset, key), ("Blue coat", None))
        other_key = vision_cache.key(asset, "makeup", False)
        _, stale = vision_cache.acquire(asset, other_key)
        ImageObservation.objects.filter(cache_key=other_key).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        _, fresh = vision_cache.acquire(asset, other_key)
        self.assertNotEqual(stale, fresh)
        vision_cache.save(other_key, stale, "Must not win")
        self.assertEqual(ImageObservation.objects.get(cache_key=other_key).description, "")
        asset.delete()
        self.assertFalse(ImageObservation.objects.exists())

    def test_scene_plan_is_cached_per_original_location_pair(self):
        first, second, location = self.upload("first"), self.upload("second"), self.upload("room")
        for original, pose in ((first, "Reclining"), (second, "Seated")):
            with patch.object(
                enhancer,
                "chat",
                return_value=json.dumps(
                    {
                        "images": [
                            {"image": 1, "description": pose},
                            {"image": 2, "description": pose + " on the same hotel bed"},
                        ]
                    }
                ),
            ) as chat:
                image_vision.analyze(
                    [original, location], time.monotonic() + 100, scene_locations=[2], reuse=True
                )
            self.assertEqual(chat.call_count, 1)
        with patch.object(enhancer, "chat") as chat:
            result = image_vision.analyze(
                [first, location], time.monotonic() + 100, scene_locations=[2], reuse=True
            )
        chat.assert_not_called()
        self.assertIn("Reclining", result[1]["description"])

    def test_transient_fallback_retries_only_that_source_with_a_compact_description(self):
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                enhancer.EnhancementError("limited", reason="rate_limit", retryable=True),
                "One occupied bed with cream linens.",
            ],
        ) as chat:
            result = image_vision.single_image(
                "data:test", 5, time.monotonic() + 70, original_picture="data:original"
            )
        self.assertEqual(result, "One occupied bed with cream linens.")
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(chat.call_args_list[0].kwargs["timeout"], (5, 25))
        self.assertIn("1800 characters", chat.call_args_list[1].args[2][2]["text"])
        self.assertEqual(
            len([item for item in chat.call_args_list[1].args[2] if item["type"] == "image_url"]), 2
        )


@override_settings(ARK_API_KEY="test-only")
class VisionProviderErrorTests(SimpleTestCase):
    def test_provider_errors_have_safe_retry_classification(self):
        import requests
        from unittest.mock import Mock

        for status, retryable, reason in (
            (429, True, "rate_limit"),
            (503, True, "provider_error"),
            (400, False, "provider_error"),
        ):
            response = Mock(status_code=status)
            with patch.object(
                enhancer.requests,
                "post",
                side_effect=requests.HTTPError("SECRET PROVIDER RESPONSE", response=response),
            ):
                with self.assertRaises(enhancer.EnhancementError) as error:
                    enhancer.chat("vision", "system", "private image content")
            self.assertEqual(error.exception.retryable, retryable)
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn("SECRET", str(error.exception))
        with patch.object(enhancer.requests, "post", side_effect=requests.Timeout("SECRET")):
            with self.assertRaises(enhancer.EnhancementError) as error:
                enhancer.chat("vision", "system", "private image content")
        self.assertTrue(error.exception.retryable)
        self.assertEqual(error.exception.reason, "timeout")
