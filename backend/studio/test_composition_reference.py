"""Photo series use composition references while focused workflows remain edits."""

import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from . import enhancer, images, replacements, test_replacements


class CompositionReferenceTests(SimpleTestCase):
    def test_base_draft_uses_composition_for_series_and_editing_for_focused_modes(self):
        plan = replacements.plan_for("source", {"model": "person", "hairstyle": "hair"})
        draft = replacements.base_prompt(plan)
        self.assertTrue(draft.startswith("Generate a new photograph using Image 1"))
        self.assertIn("Use Image 2 for the primary person's identity", draft)
        self.assertIn("Use Image 3 for the hairstyle and hair color", draft)
        self.assertNotIn("Edit Image 1", draft)
        for workflow in ("makeup", "wardrobe"):
            with self.subTest(workflow=workflow):
                self.assertTrue(
                    replacements.base_prompt(plan, workflow=workflow).startswith("Edit Image 1")
                )

    def test_source_geometry_is_observed_even_without_a_location_donor(self):
        plan = replacements.plan_for("source", {"model": "person"})
        focus = replacements.reference_focus(plan, False)
        for detail in ("Composition reference", "crop", "subject position", "contact points"):
            self.assertIn(detail, focus[1])
        self.assertIn("facial structure", focus[2])
        focused = replacements.reference_focus(plan, False, workflow="makeup")
        self.assertNotIn(1, focused)


@override_settings(
    DEBUG=True,
    GENERATION_PROVIDER="mock",
    CREDITS_ENABLED=False,
    S3_BUCKET="",
    SAVE_IMAGES_TO_S3=True,
)
class CompositionReferenceAPITests(TestCase):
    setUp = test_replacements.ReplacementTests.setUp
    upload = test_replacements.ReplacementTests.upload

    def test_reference_repair_keeps_composition_role_and_focused_edit_roles(self):
        source, donor = self.upload("composition"), self.upload("outfit")
        for workflow in ("replacement", "wardrobe", "makeup"):
            with self.subTest(workflow=workflow):
                role = "makeup" if workflow == "makeup" else "clothing"
                with patch.object(
                    enhancer,
                    "chat",
                    side_effect=[
                        json.dumps(
                            {
                                "images": [
                                    {"image": 1, "description": "Seated portrait, side view."},
                                    {"image": 2, "description": "Blue jacket and pink makeup."},
                                ]
                            }
                        ),
                        "Use Image 9 for the reference.",
                        "Create the seated portrait from Image 1, using Image 2 for the selected appearance.",
                    ],
                ) as chat:
                    response = self.client.post(
                        "/api/images/replacements/prompt",
                        {
                            "original": str(source.pk),
                            "workflow": workflow,
                            "replacements": {role: str(donor.pk)},
                        },
                        format="json",
                    )
                self.assertEqual(response.status_code, 200, response.data)
                expected_role = (
                    images.COMPOSITION_REFERENCE_ROLE
                    if workflow == "replacement"
                    else "the original photograph to edit"
                )
                writer = json.loads(chat.call_args_list[1].args[2])
                repair = json.loads(chat.call_args_list[2].args[2])
                for context in (writer, repair):
                    self.assertEqual(context["originalReferenceRole"], expected_role)
                    self.assertEqual(images.known_reference_roles(context)[1], [expected_role])
                if workflow == "replacement":
                    self.assertTrue(writer["draft"].startswith("Generate a new photograph"))
                else:
                    self.assertTrue(writer["draft"].startswith("Edit Image 1"))
