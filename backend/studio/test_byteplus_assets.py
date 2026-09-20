import io
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from . import byteplus_assets, provider
from .models import Asset, Chunk, Project, Run


@override_settings(BYTEPLUS_ASSET_PROJECT="default")
class BytePlusAssetsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("catalog-user")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.item = {
            "Id": "asset-test",
            "Name": "Character",
            "AssetType": "Image",
            "Status": "Active",
        }

    def test_sync_idempotent_and_removed_assets_unavailable(self):
        with patch.object(byteplus_assets, "catalog", return_value={"asset-test": self.item}):
            first = self.client.post("/api/characters/sync", {}, format="json")
            second = self.client.post("/api/characters/sync", {}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, second.data)
        self.assertEqual(Asset.objects.count(), 1)
        self.assertTrue(first.data[0]["url"].startswith("/api/characters/"))
        self.assertNotIn("provider_id", first.data[0])
        with patch.object(byteplus_assets, "catalog", return_value={}):
            self.assertEqual(self.client.post("/api/characters/sync").data, [])
        self.assertEqual(Asset.objects.get().provider_status, "Unavailable")

    def test_failure_keeps_cache_and_does_not_leak_provider_error(self):
        asset = Asset.objects.create(
            owner=self.user, kind="character", name="Keep", provider_id="old"
        )
        with patch.object(
            byteplus_assets, "catalog", side_effect=byteplus_assets.CatalogError("Unavailable")
        ):
            self.assertEqual(self.client.post("/api/characters/sync").status_code, 502)
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        with patch.object(byteplus_assets, "call") as call:
            self.client.force_authenticate(None)
            self.assertEqual(self.client.post("/api/characters/sync").status_code, 403)
            call.assert_not_called()

    def test_pagination_dedup_and_image_filter(self):
        page = [{**self.item, "Id": f"asset-{i}"} for i in range(100)]
        with patch.object(
            byteplus_assets,
            "call",
            side_effect=[
                {"Items": page, "TotalCount": 101},
                {"Items": [self.item], "TotalCount": 101},
                {
                    "Items": [self.item, {**self.item, "Id": "video", "AssetType": "Video"}],
                    "TotalCount": 2,
                },
            ],
        ) as call:
            result = byteplus_assets.catalog()
        self.assertEqual(len(result), 101)
        self.assertEqual(call.call_args_list[1].args[1]["PageNumber"], 2)
        self.assertNotIn("video", result)

    @override_settings(BYTEPLUS_ACCESS_KEY_ID="test-key", BYTEPLUS_SECRET_ACCESS_KEY="test-secret")
    def test_signed_server_request_and_redacted_errors(self):
        response = Mock()
        response.json.return_value = {"Result": {"Items": []}}
        with patch.object(byteplus_assets.requests, "post", return_value=response) as post:
            byteplus_assets.call("ListAssets", {"ProjectName": "default"})
        args = post.call_args.kwargs
        self.assertIn("Credential=test-key/", args["headers"]["Authorization"])
        self.assertIn("Signature=", args["headers"]["Authorization"])
        self.assertFalse(args["allow_redirects"])
        response.json.return_value = {"ResponseMetadata": {"Error": {"Message": "private-url"}}}
        with patch.object(byteplus_assets.requests, "post", return_value=response):
            with self.assertRaises(byteplus_assets.CatalogError) as error:
                byteplus_assets.call("ListAssets", {})
        self.assertNotIn("private-url", str(error.exception))

    def test_preview_is_private_and_reencodes_image(self):
        asset = Asset.objects.create(
            owner=self.user, kind="character", name="Hero", provider_id="asset-test"
        )
        url = f"/api/characters/{asset.pk}/preview"

        def download(_url, target, **kwargs):
            self.assertEqual(kwargs["max_bytes"], 20 * 1024 * 1024)
            Image.new("RGB", (900, 1200)).save(Path(target), "PNG")

        with (
            patch.object(
                byteplus_assets,
                "detail",
                return_value={**self.item, "URL": "https://private-host/image"},
            ),
            patch.object(provider, "download", side_effect=download),
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(response.content)).size, (480, 640))
        other = get_user_model().objects.create_user("other-catalog-user")
        self.client.force_authenticate(other)
        with patch.object(byteplus_assets, "detail") as detail:
            self.assertEqual(self.client.get(url).status_code, 404)
            detail.assert_not_called()

    def test_generation_uses_private_asset_uri(self):
        asset = Asset.objects.create(
            owner=self.user,
            kind="character",
            name="Hero",
            provider_id="asset-test",
            provider_status="Active",
        )
        project = Project.objects.create(owner=self.user)
        import uuid

        run = Run.objects.create(
            project=project,
            part="freeform",
            snapshot={"character": str(asset.pk), "model": "test"},
            request_key=uuid.uuid4(),
        )
        chunk = Chunk.objects.create(
            run=run, position=0, start=0, duration=10, prompt="test", cost=0
        )
        with patch.object(provider.storage, "url") as storage_url:
            data = provider.payload(chunk)
            storage_url.assert_not_called()
        self.assertEqual(data["content"][1]["image_url"]["url"], "asset://asset-test")
        asset.provider_status = "Unavailable"
        asset.save()
        with self.assertRaises(provider.Rejected):
            provider.payload(chunk)
