import io
import base64
import json
import shutil
import subprocess
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


class PromptEnhancementTests(unittest.TestCase):
    def call_endpoint(self, data, response=None, status=200, key="test-key", error=None):
        handler = FakeHandler()
        body = json.dumps(data).encode()
        handler.rfile = io.BytesIO(body)
        handler.headers["Content-Length"] = str(len(body))
        if response is None:
            response = {"choices": [{"finish_reason": "stop", "message": {"content": "Кот прыгает на подоконник. Камера следует за движением."}}]}
        with mock.patch.object(web_app, "get_secret", side_effect=lambda names: key if names == web_app.PROVIDERS["byteplus"]["token_names"] else ""), \
                mock.patch.object(web_app, "request_json", return_value=(status, response), side_effect=error) as request:
            web_app.SeedanceHandler.handle_enhance_prompt(handler)
        return handler.status, json.loads(handler.wfile.getvalue()), request

    def test_rewrite_uses_ark_chat_and_separate_model_with_generation_context(self):
        status, result, request = self.call_endpoint({
            "prompt": "кот прыгает", "duration": 10, "aspectRatio": "9:16",
            "generateAudio": True, "references": ["@image1"],
            "model": "video-endpoint", "baseUrl": "https://untrusted.example", "apiKey": "client-key",
        })
        self.assertEqual(status, 200)
        self.assertIn("Кот", result["prompt"])
        method, url, key, payload = request.call_args.args
        self.assertEqual((method, url, key), ("POST", "https://ark.ap-southeast.bytepluses.com/api/v3/chat/completions", "test-key"))
        self.assertEqual(payload["model"], web_app.DEEPSEEK_MODEL)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        context = json.loads(payload["messages"][1]["content"])
        self.assertEqual(context, {"draft": "кот прыгает", "durationSeconds": 10, "aspectRatio": "9:16", "generateAudio": True, "availableReferenceTags": ["@image1"]})
        self.assertNotIn("raw", result)

    def test_dedicated_deepseek_endpoint_overrides_default(self):
        with mock.patch.dict(web_app.os.environ, {"DEEPSEEK_ENDPOINT_ID": "ep-chat", "ARK_ENDPOINT_ID": "ep-video"}):
            self.assertEqual(web_app.build_enhance_payload({"prompt": "кот"})["model"], "ep-chat")

    def test_invalid_inputs_never_call_provider(self):
        for data in [None, [], {"prompt": " "}, {"prompt": 123}, {"prompt": "x" * 12001},
                     {"prompt": "кот", "duration": 31}, {"prompt": "кот", "duration": None},
                     {"prompt": "кот", "duration": 5.5}, {"prompt": "кот", "mode": "image"},
                     {"prompt": "кот", "aspectRatio": "bad"}, {"prompt": "кот", "generateAudio": "false"},
                     {"prompt": "кот", "references": ["https://example.com"]}]:
            with self.subTest(data=str(data)[:100]):
                status, result, request = self.call_endpoint(data)
                self.assertEqual(status, 400)
                self.assertIn("error", result)
                request.assert_not_called()

    def test_missing_key_does_not_call_provider(self):
        status, result, request = self.call_endpoint({"prompt": "кот"}, key="")
        self.assertEqual(status, 503)
        self.assertIn("ARK_API_KEY", result["error"])
        request.assert_not_called()

    def test_oversized_body_is_rejected_before_reading(self):
        handler = FakeHandler()
        handler.headers["Content-Length"] = "100001"
        handler.rfile = mock.Mock()
        with mock.patch.object(web_app, "request_json") as request:
            web_app.SeedanceHandler.handle_enhance_prompt(handler)
        self.assertEqual(handler.status, 413)
        self.assertTrue(handler.close_connection)
        handler.rfile.read.assert_not_called()
        request.assert_not_called()

    def test_provider_errors_and_timeout_leave_no_replacement(self):
        for provider_status in (401, 403, 404, 429, 500):
            with self.subTest(status=provider_status):
                status, result, _ = self.call_endpoint({"prompt": "кот"}, status=provider_status, response={"error": {"message": "Provider error"}})
                self.assertEqual(status, 502)
                self.assertNotIn("prompt", result)
        for error, expected in [(TimeoutError(), 504), (RuntimeError("Network error"), 502)]:
            status, result, _ = self.call_endpoint({"prompt": "кот"}, error=error)
            self.assertEqual(status, expected)
            self.assertNotIn("prompt", result)

    def test_empty_truncated_or_malformed_responses_are_rejected(self):
        for response in [{}, None, {"choices": []}, {"choices": [None]},
                         {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
                         {"choices": [{"finish_reason": "stop", "message": {"content": " "}}]},
                         {"choices": [{"finish_reason": "stop", "message": None}]}]:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                web_app.enhanced_prompt_from_response(response, "кот", [])

    def test_reference_links_and_numbering_must_survive(self):
        original = "@image1 с @video2 и @audio1 https://example.com/ref.png asset://hero"
        def response(text):
            return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
        self.assertEqual(web_app.enhanced_prompt_from_response(response(original + " Камера приближается."), original, []), original + " Камера приближается.")
        self.assertEqual(web_app.enhanced_prompt_from_response(response(original + "."), original, []), original + ".")
        for changed in [original.replace("@image1", "@image2"), original.replace("asset://hero", "asset://other"), original + " @image9", original + " https://invented.example"]:
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                web_app.enhanced_prompt_from_response(response(changed), original, ["@image2"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for editor interaction checks")
    def test_editor_interactions(self):
        result = subprocess.run(["node", "test_prompt_editor.js"], input=web_app.JS, text=True, capture_output=True, cwd=web_app.ROOT_DIR)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


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
                             ("/api/enhance-prompt", "POST"),
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
