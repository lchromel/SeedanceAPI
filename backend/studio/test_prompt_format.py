import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from . import enhancer, prompt_format, replacements, test_replacements


def brief():
    return prompt_format.render(
        dict(
            zip(
                prompt_format.HEADINGS,
                (
                    "Photorealistic editorial portrait.",
                    "The person from Image 1, preserving their age and facial identity.",
                    "Eye-level, original reclining crop, 9:16.",
                    "One occupied bed with cream linens and a walnut headboard, warm beige walls visible behind it.",
                    "Visible pores, fine baby hairs and natural linen folds.",
                    "Reclining on the mattress, preserving the original expression and body contact.",
                    "Soft diffused window light from the left, with natural catchlights.",
                    "Extra beds, warped hands, excessive skin smoothing.",
                ),
            )
        )
    )


class PromptFormatTests(SimpleTestCase):
    def test_complete_sections_are_canonical_and_duplicate_or_missing_headings_are_rejected(self):
        text = brief()
        self.assertEqual(prompt_format.render(prompt_format.parse(text)), text)
        self.assertIsNone(prompt_format.parse(text.replace("LIGHTING", "OTHER")))
        self.assertIsNone(prompt_format.parse(text + "\n\nLIGHTING\nDuplicate"))
        self.assertEqual(len(prompt_format.parse("```text\n" + text + "\n```")), 8)
        marked = text
        for heading in prompt_format.HEADINGS:
            marked = marked.replace(heading, "**" + heading + ":**")
        self.assertEqual(prompt_format.render(prompt_format.parse(marked)), text)

    def test_controls_stay_inside_sections_without_generic_location_boilerplate(self):
        data = {
            "keepTattoos": False,
            "identity": {"eyeColor": "blue", "details": "Freckles across the nose."},
            "locationDescriptionOnly": True,
        }
        result = replacements.apply_controls(brief(), data)
        sections = prompt_format.parse(result)
        self.assertEqual(len(sections), 8)
        self.assertIn("Blue irises.", sections["SUBJECT + AGE"])
        self.assertIn("free of tattoos", sections["MICRO-DETAILS"])
        self.assertIn("cream linens", sections["SCENE + SETTING"])
        self.assertNotIn("Location geometry:", result)
        self.assertNotIn("Fixed identity details:", result)
        self.assertEqual(replacements.apply_controls(result, data), result)
        legacy = "Edit the one occupied bed with cream linens.\n\nLocation geometry:\nUse Image 1 as the camera anchor."
        self.assertEqual(
            replacements.scene_draft(legacy), "Edit the one occupied bed with cream linens."
        )


@override_settings(
    DEBUG=True,
    GENERATION_PROVIDER="mock",
    CREDITS_ENABLED=False,
    S3_BUCKET="",
    SAVE_IMAGES_TO_S3=True,
)
class StructuredPromptAPITests(TestCase):
    setUp = test_replacements.ReplacementTests.setUp
    upload = test_replacements.ReplacementTests.upload

    def test_missing_format_is_repaired_as_a_full_eight_section_prompt(self):
        original = self.upload("original")
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"Person reclining on one cream bed."}]}',
                "Edit Image 1 on the one cream bed.",
                brief(),
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/enhance",
                {
                    "prompt": "Keep the portrait",
                    "references": [str(original.pk)],
                    "structuredPrompt": True,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(prompt_format.parse(response.data["prompt"])), 8)
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(
            json.loads(chat.call_args_list[2].args[2])["referenceRepair"]["requiredSections"],
            list(prompt_format.HEADINGS),
        )

    def test_replacement_role_completion_preserves_requested_structure(self):
        original, outfit = self.upload("original"), self.upload("outfit")
        with patch.object(
            enhancer,
            "chat",
            side_effect=[
                '{"images":[{"image":1,"description":"Portrait"},{"image":2,"description":"A blue jacket"}]}',
                brief(),
            ],
        ) as chat:
            response = self.client.post(
                "/api/images/replacements/prompt",
                {
                    "original": str(original.pk),
                    "replacements": {"clothing": str(outfit.pk)},
                    "structuredPrompt": True,
                    "identity": {"eyeColor": "blue"},
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        sections = prompt_format.parse(response.data["prompt"])
        self.assertEqual(len(sections), 8)
        self.assertIn("Use Image 2 for the clothing and accessories.", sections["SUBJECT + AGE"])
        self.assertIn("Blue irises.", sections["SUBJECT + AGE"])
        self.assertEqual(chat.call_count, 2)
