import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from . import storage, views
from .models import Asset


class PreviewTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        settings = override_settings(S3_BUCKET='', DEBUG=True, MEDIA_ROOT=Path(self.folder.name))
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_user('preview-owner')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_existing_image_gets_small_private_cached_preview(self):
        asset = Asset.objects.create(owner=self.user, kind='location', name='Room',
                                     key=f'{self.user.pk}/assets/room.jpg', mime='image/jpeg')
        source = io.BytesIO()
        Image.effect_noise((2048, 1536), 80).convert('RGB').save(source, 'JPEG', quality=95)
        original = source.getvalue()
        storage.put(asset.key, io.BytesIO(original), 'image/jpeg')
        url = views.asset_data(asset)['url']
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(first.content)).size, (640, 480))
        self.assertLess(len(first.content), len(original) // 4)
        self.assertEqual((Path(self.folder.name) / asset.key).read_bytes(), original)
        with patch.object(storage, 'fetch', wraps=storage.fetch) as fetch:
            second = self.client.get(url)
            self.assertEqual(second.content, first.content)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0], views.preview_key(asset))
        self.assertEqual(second['Cache-Control'], 'no-store')
        self.client.force_authenticate(get_user_model().objects.create_user('outsider'))
        with patch.object(storage, 'fetch') as fetch:
            self.assertEqual(self.client.get(url).status_code, 404)
            fetch.assert_not_called()
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(url).status_code, 403)
