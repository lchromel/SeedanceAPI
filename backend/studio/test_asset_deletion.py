import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Asset, Project, Run


class AssetDeletionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('asset-owner')
        self.other = get_user_model().objects.create_user('other-owner')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.asset = Asset.objects.create(owner=self.user, kind='clothing', name='Boots', key='1/assets/test.jpg', mime='image/jpeg')
        self.project = Project.objects.create(owner=self.user, config={'clothing': [str(self.asset.pk)], 'location': None})
        self.url = f'/api/assets/{self.asset.pk}'

    def test_removes_selection_and_revokes_preview(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Asset.objects.filter(pk=self.asset.pk).exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.config['clothing'], [])
        self.assertEqual(self.project.revision, 2)
        self.assertEqual(response.data['projects'][0]['previousRevision'], 1)
        self.assertEqual(self.client.get(self.url + '/preview').status_code, 404)

    def test_cannot_delete_another_users_asset(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())

    def test_pending_generation_keeps_references_and_completed_video_survives(self):
        run = Run.objects.create(project=self.project, part='freeform', snapshot={'reference_ids': [str(self.asset.pk)]}, request_key=uuid.uuid4())
        self.assertEqual(self.client.delete(self.url).status_code, 409)
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())
        run.state = 'ready'
        run.save()
        self.assertEqual(self.client.delete(self.url).status_code, 200)
        self.assertTrue(Run.objects.filter(pk=run.pk).exists())

    def test_location_is_cleared_and_character_deletion_is_not_supported(self):
        self.asset.kind = 'location'
        self.asset.save()
        self.project.config = {'clothing': [], 'location': str(self.asset.pk)}
        self.project.save()
        self.assertEqual(self.client.delete(self.url).status_code, 200)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.config['location'])
        character = Asset.objects.create(owner=self.user, kind='character', name='Hero')
        self.assertEqual(self.client.delete(f'/api/assets/{character.pk}').status_code, 404)
