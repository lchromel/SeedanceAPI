import io
import base64
import json
import unittest
from unittest import mock

import web_app


class FakeHandler:
    require_authentication = web_app.SeedanceHandler.require_authentication

    def __init__(self):
        self.command = "GET"
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass


class ImageGenerationTests(unittest.TestCase):
    def test_text_only_generation_needs_no_assets(self):
        payload = web_app.build_image_payload("byteplus", {"prompt": "A coastline"})
        self.assertNotIn("image", payload)

    def test_asset_reference_is_rejected_before_fetching_images(self):
        for uri in ("asset://asset-20260906230938-4qpst", "ASSET://example"):
            with self.subTest(uri=uri), mock.patch.object(web_app, "remote_image_as_data_url") as fetch:
                with self.assertRaisesRegex(ValueError, "исходный файл"):
                    web_app.build_image_payload("byteplus", {
                        "prompt": "A coastline", "imageUrls": ["https://example.com/a.png", uri]
                    })
                fetch.assert_not_called()

    def test_regular_references_are_preserved_in_order(self):
        image = "data:image/png;base64,aGVsbG8="
        with mock.patch.object(web_app, "remote_image_as_data_url", return_value=image) as fetch:
            payload = web_app.build_image_payload("byteplus", {
                "prompt": "A coastline", "imageUrls": ["https://example.com/a.png", image]
            })
        self.assertEqual(payload["image"], [image, image])
        self.assertEqual(fetch.call_args_list, [mock.call("https://example.com/a.png"), mock.call(image)])


class AssetLibraryTests(unittest.TestCase):
    def call_endpoint(self, params, responses):
        handler = FakeHandler()

        def fake_api(action, payload=None, timeout=45):
            if action == "ListAssetGroups":
                return {"Items": [{"Id": "person-group"}], "TotalCount": 1}
            group_type = payload["Filter"]["GroupType"]
            return responses[group_type]

        with mock.patch.object(web_app, "call_byteplus_asset_api", side_effect=fake_api):
            web_app.SeedanceHandler.handle_list_assets(handler, params)

        self.assertEqual(handler.status, 200)
        return json.loads(handler.wfile.getvalue())

    def test_unfiltered_library_combines_aigc_and_real_person_assets(self):
        payload = self.call_endpoint(
            {"projectName": ["default"]},
            {
                "AIGC": {
                    "Items": [{"Id": "aigc-1", "CreateTime": "2026-09-06T12:00:00Z"}],
                    "TotalCount": 1,
                },
                "LivenessFace": {
                    "Items": [{"Id": "person-1", "CreateTime": "2026-09-05T12:00:00Z"}],
                    "TotalCount": 1,
                },
            },
        )

        self.assertEqual([item["Id"] for item in payload["assets"]], ["aigc-1", "person-1"])
        self.assertEqual(payload["assetCount"], 2)

    def test_selected_real_person_group_does_not_request_aigc_assets(self):
        payload = self.call_endpoint(
            {"projectName": ["default"], "groupId": ["person-group"]},
            {
                "LivenessFace": {
                    "Items": [{"Id": "person-1", "CreateTime": "2026-09-05T12:00:00Z"}],
                    "TotalCount": 1,
                }
            },
        )

        self.assertEqual([item["Id"] for item in payload["assets"]], ["person-1"])
        self.assertEqual(payload["assetCount"], 1)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web_app, "web_credentials", return_value=("test-user", "test-password"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, method="GET", authorization=None):
        handler = FakeHandler()
        handler.command = method
        handler.path = path
        if authorization is not None:
            handler.headers["Authorization"] = authorization
        if method == "GET":
            web_app.SeedanceHandler.do_GET(handler)
        else:
            web_app.SeedanceHandler.do_POST(handler)
        return handler

    def test_page_api_and_uploads_require_authentication(self):
        for path, method in [("/", "GET"), ("/app.js", "GET"), ("/api/assets", "GET"),
                             ("/uploads/test.png", "GET"), ("/api/generate", "POST"),
                             ("/api/upload-reference", "POST")]:
            with self.subTest(path=path):
                response = self.request(path, method)
                self.assertEqual(response.status, 401)
                self.assertIn("Basic", response.headers["WWW-Authenticate"])

    def test_correct_credentials_open_page(self):
        auth = "Basic " + base64.b64encode(b"test-user:test-password").decode()
        response = self.request("/", authorization=auth)
        self.assertEqual(response.status, 200)
        self.assertIn(b"Seedance", response.wfile.getvalue())

    def test_invalid_credentials_and_malformed_headers_are_denied(self):
        for value in ("Basic !!!", "Basic " + base64.b64encode(b"test-user:wrong").decode(), "Bearer wrong"):
            with self.subTest(value=value):
                self.assertEqual(self.request("/", authorization=value).status, 401)

    def test_health_public_but_missing_configuration_fails_closed(self):
        with mock.patch.object(web_app, "web_credentials", return_value=("", "")):
            self.assertEqual(self.request("/health").status, 200)
            self.assertEqual(self.request("/").status, 503)
            self.assertEqual(self.request("/api/generate", "POST").status, 503)

    def test_signed_file_access_without_login(self):
        url = web_app.signed_upload_url("example.png")
        with mock.patch.object(web_app, "file_response") as serve:
            self.request(url)
            serve.assert_called_once()
        self.assertEqual(self.request(url.replace("example.png", "another.png")).status, 401)
        self.assertEqual(self.request(url.replace("/uploads/example.png", "/api/assets")).status, 401)
        self.assertEqual(self.request(url, "POST").status, 401)

    def test_expired_links_and_password_rotation_revoke_file_access(self):
        with mock.patch.object(web_app.time, "time", return_value=1000):
            old_url = web_app.signed_upload_url("example.png")
        self.assertEqual(self.request(old_url).status, 401)
        current_url = web_app.signed_upload_url("example.png")
        with mock.patch.object(web_app, "web_credentials", return_value=("test-user", "changed")):
            self.assertEqual(self.request(current_url).status, 401)


if __name__ == "__main__":
    unittest.main()
