import io
import json
import unittest
from unittest import mock

import web_app


class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass


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


if __name__ == "__main__":
    unittest.main()
