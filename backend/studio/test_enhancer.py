import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from . import enhancer, provider, references, services
from .models import Asset, Project, Wallet


@override_settings(CREDITS_PER_SECOND=1, GENERATION_PROVIDER="mock", PROVIDER_MAX_SECONDS=30)
class EnhancerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("writer")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.character = Asset.objects.create(
            owner=self.user, kind="character", name="Person", key="identity"
        )
        self.location = Asset.objects.create(
            owner=self.user,
            kind="location",
            category="interior",
            name="Room",
            key="room",
            description="Concrete floor and side window.",
            analysis_status="ready",
        )
        self.boots = Asset.objects.create(
            owner=self.user,
            kind="clothing",
            category="boots",
            name="Boots",
            key="boots",
            description="Tall green rubber boots with ridged soles.",
            analysis_status="ready",
        )
        self.vest = Asset.objects.create(
            owner=self.user,
            kind="clothing",
            category="vest",
            name="Vest",
            key="vest",
            description="Quilted blue vest with a zip.",
            analysis_status="ready",
        )
        self.config = {
            "character": str(self.character.pk),
            "location": str(self.location.pk),
            "clothing": [str(self.boots.pk), str(self.vest.pk)],
            "duration": 30,
            "action": "Walk slowly. Render the boots in black rubber.",
            "speech": "Привет",
            "motions": [],
        }

    def test_reference_order_matches_payload_and_snapshot_is_immutable(self):
        import uuid

        Wallet.objects.create(user=self.user, available=1000)
        project = Project.objects.create(owner=self.user, config=self.config)
        run = services.generate(self.user, project.pk, "freeform", uuid.uuid4())
        self.boots.description = "Unrelated later edit"
        self.boots.save()
        with patch.object(provider.storage, "url", side_effect=lambda key, **kw: key):
            payload = provider.payload(run.chunks.first())
        self.assertEqual(
            [x["image_url"]["url"] for x in payload["content"][1:]],
            ["identity", "room", "boots", "vest"],
        )
        text = payload["content"][0]["text"]
        self.assertIn("@Image2: use for the location", text)
        self.assertIn("@Image3: use exclusively for the boots", text)
        self.assertIn("Tall green rubber boots", text)
        self.assertNotIn("Unrelated later edit", text)
        self.assertIn("Render the boots in black rubber", text)
        self.assertIn("Привет", text)

    def test_without_location_numbering_is_contiguous(self):
        assets = references.ordered_assets(self.user, {**self.config, "location": None})
        self.assertIn("@Image2: use exclusively for the boots", references.instructions(assets))
        self.assertNotIn("@Image4", references.instructions(assets))

    def test_unanalyzed_reference_blocks_generation_before_reservation(self):
        import uuid

        Wallet.objects.create(user=self.user, available=100)
        self.boots.analysis_status = "failed"
        self.boots.save()
        project = Project.objects.create(owner=self.user, config=self.config)
        with self.assertRaises(ValidationError):
            services.generate(self.user, project.pk, "freeform", uuid.uuid4())
        self.assertEqual(self.user.wallet.available, 100)
        self.assertEqual(project.runs.count(), 0)

    def test_enhancement_uses_descriptions_and_keeps_dialogue_separate(self):
        original = self.config.copy()
        with patch.object(
            enhancer, "chat", return_value="The person walks naturally wearing black rubber boots."
        ) as chat:
            response = self.client.post("/api/prompt/enhance", self.config, format="json")
        self.assertEqual(response.status_code, 200)
        context = json.loads(chat.call_args.args[2])
        self.assertIn("Quilted blue vest", context["referenceInstructions"])
        self.assertIn("@Image4", response.data["prompt"])
        self.assertEqual(self.config, original)
        self.assertNotIn("Привет", response.data["action"])

    def test_foreign_references_cannot_reach_model(self):
        other = get_user_model().objects.create_user("outsider")
        self.boots.owner = other
        self.boots.save()
        with patch.object(enhancer, "chat") as chat:
            self.assertEqual(
                self.client.post("/api/prompt/enhance", self.config, format="json").status_code, 400
            )
            chat.assert_not_called()
        self.assertEqual(
            self.client.post(
                f"/api/assets/{self.boots.pk}/analyze", {"category": "boots"}
            ).status_code,
            404,
        )

    def test_invalid_model_reference_rejected_and_original_unchanged(self):
        project = Project.objects.create(owner=self.user, config=self.config)
        with patch.object(enhancer, "chat", return_value="Use @Image99 for the outfit."):
            self.assertEqual(
                self.client.post("/api/prompt/enhance", self.config, format="json").status_code, 400
            )
        project.refresh_from_db()
        self.assertEqual(project.config, self.config)

    def image_file(self):
        image = io.BytesIO()
        Image.new("RGB", (100, 100), "green").save(image, "PNG")
        image.seek(0)
        image.name = "upload.png"
        return image

    def test_upload_requires_category_and_auto_names_from_observations(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(MEDIA_ROOT=Path(folder), S3_BUCKET="", DEBUG=True),
        ):
            with patch.object(
                enhancer,
                "chat",
                side_effect=[
                    "Green rubber boots.",
                    '{"name":"Green Rubber Boots","description":"Tall green rubber boots."}',
                ],
            ) as chat:
                response = self.client.post(
                    "/api/assets",
                    {"kind": "clothing", "category": "boots", "file": self.image_file()},
                    format="multipart",
                )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data["name"], "Green Rubber Boots")
            self.assertEqual(response.data["analysisStatus"], "ready")
            self.assertTrue(
                chat.call_args_list[0]
                .args[2][1]["image_url"]["url"]
                .startswith("data:image/jpeg;base64,")
            )
            self.assertIn("Green rubber boots", chat.call_args_list[1].args[2])
            with patch.object(enhancer, "chat") as chat:
                response = self.client.post(
                    "/api/assets",
                    {"kind": "clothing", "category": "interior", "file": self.image_file()},
                    format="multipart",
                )
                self.assertEqual(response.status_code, 400)
                chat.assert_not_called()

    def test_model_failure_preserves_uploaded_file_and_supports_retry(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(MEDIA_ROOT=Path(folder), S3_BUCKET="", DEBUG=True),
        ):
            with patch.object(enhancer, "chat", side_effect=enhancer.EnhancementError("Try again")):
                response = self.client.post(
                    "/api/assets",
                    {"kind": "location", "category": "interior", "file": self.image_file()},
                    format="multipart",
                )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data["analysisStatus"], "failed")
            asset = Asset.objects.get(pk=response.data["id"])
            self.assertTrue((Path(folder) / asset.key).exists())
            with patch.object(
                enhancer,
                "chat",
                side_effect=["Green walls.", '{"name":"Green Room","description":"Green walls."}'],
            ):
                retried = self.client.post(
                    f"/api/assets/{asset.pk}/analyze", {"category": "interior"}, format="json"
                )
            self.assertEqual(retried.status_code, 200)
            self.assertEqual(retried.data["analysisStatus"], "ready")
