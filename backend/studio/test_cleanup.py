import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from . import provider
from .models import Chunk, Project, Run, Ledger
from .tasks import cleanup_provider_task, dispatch, finish, process_chunk


class CleanupTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("cleanup")
        project = Project.objects.create(owner=user)
        run = Run.objects.create(
            project=project, part="freeform", request_key=uuid.uuid4(),
            snapshot={"provider": "byteplus"},
        )
        self.chunk = Chunk.objects.create(
            run=run, position=0, start=0, duration=4, prompt="test", cost=0,
            state="generating", provider_id="task-test",
        )

    def ready(self):
        finish(self.chunk.id, "ready", "outputs/test.mp4")
        self.chunk.refresh_from_db()

    def test_output_pipeline_only_marks_pending_after_storage(self):
        def download(url, path):
            Path(path).write_bytes(b"video")

        with (
            patch("studio.tasks.provider.status", return_value=("succeeded", "https://video")),
            patch("studio.tasks.provider.download", side_effect=download),
            patch("studio.tasks.media.probe"),
            patch("studio.tasks.storage.put", side_effect=OSError("storage down")) as put,
            patch("studio.tasks.provider.delete_task") as delete,
        ):
            process_chunk(self.chunk.id)
            self.chunk.refresh_from_db()
            self.assertEqual(self.chunk.cleanup_state, "")
            self.assertEqual(self.chunk.state, "generating")
            self.assertFalse(Ledger.objects.exists())
            put.side_effect = None
            process_chunk(self.chunk.id)
            self.chunk.refresh_from_db()
            self.assertEqual(self.chunk.state, "ready")
            self.assertEqual(self.chunk.cleanup_state, "pending")
            self.assertTrue(self.chunk.output_key)
            delete.assert_not_called()

    def test_pending_rolls_back_with_result(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.ready()
                raise RuntimeError("rollback")
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.cleanup_state, "")
        self.assertEqual(self.chunk.state, "generating")
        self.assertFalse(Ledger.objects.exists())

    @patch("studio.tasks.provider.delete_task")
    def test_success_and_duplicate_delivery(self, delete):
        self.ready()
        cleanup_provider_task(self.chunk.id)
        cleanup_provider_task(self.chunk.id)
        delete.assert_called_once_with("task-test")
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.cleanup_state, "deleted")
        self.assertIsNotNone(self.chunk.provider_deleted_at)
        self.assertEqual(self.chunk.state, "ready")
        self.assertEqual(Ledger.objects.count(), 1)

    @patch("studio.tasks.provider.delete_task", side_effect=requests.Timeout)
    def test_failure_retries_without_affecting_result(self, delete):
        self.ready()
        cleanup_provider_task(self.chunk.id)
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.cleanup_state, "pending")
        self.assertEqual(self.chunk.cleanup_attempts, 1)
        self.assertGreater(self.chunk.cleanup_next_attempt, timezone.now())
        self.assertEqual(self.chunk.state, "ready")
        self.assertEqual(self.chunk.output_key, "outputs/test.mp4")
        cleanup_provider_task(self.chunk.id)
        self.assertEqual(delete.call_count, 1)
        Chunk.objects.filter(pk=self.chunk.pk).update(cleanup_next_attempt=timezone.now())
        delete.side_effect = None
        cleanup_provider_task(self.chunk.id)
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.cleanup_state, "deleted")
        self.assertEqual(Ledger.objects.count(), 1)

    @patch("studio.tasks.assemble.delay")
    @patch("studio.tasks.cleanup_provider_task.delay")
    def test_dispatch_recovers_expired_lease(self, delay, assemble):
        self.ready()
        Chunk.objects.filter(pk=self.chunk.pk).update(
            cleanup_lease_until=timezone.now() + timedelta(minutes=2)
        )
        dispatch()
        delay.assert_not_called()
        Chunk.objects.filter(pk=self.chunk.pk).update(
            cleanup_lease_until=timezone.now() - timedelta(seconds=1)
        )
        dispatch()
        delay.assert_called_once_with(str(self.chunk.pk))

    @patch("studio.tasks.provider.delete_task")
    def test_worker_guards(self, delete):
        self.ready()
        for fields in (
            {"state": "generating"}, {"output_key": ""}, {"provider_id": ""},
            {"cleanup_lease_until": timezone.now() + timedelta(minutes=1)},
        ):
            with transaction.atomic():
                Chunk.objects.filter(pk=self.chunk.pk).update(**fields)
                cleanup_provider_task(self.chunk.id)
                transaction.set_rollback(True)
        delete.assert_not_called()

    def test_mock_finish_does_not_request_cleanup(self):
        run = self.chunk.run
        run.snapshot = {"provider": "mock"}
        run.save()
        self.ready()
        self.assertEqual(self.chunk.cleanup_state, "")


class DeleteAdapterTests(SimpleTestCase):
    @patch("studio.provider.requests.delete")
    def test_success_and_already_missing(self, delete):
        for status in (200, 204, 404):
            delete.return_value.status_code = status
            provider.delete_task("task/id")
        self.assertTrue(delete.call_args.args[0].endswith("task%2Fid"))
        self.assertFalse(delete.call_args.kwargs["allow_redirects"])
        self.assertEqual(delete.call_args.kwargs["timeout"], (10, 30))

    @patch("studio.provider.requests.delete")
    def test_errors_and_redirects_are_not_success(self, delete):
        for status in (301, 401, 429, 500):
            response = requests.Response()
            response.status_code = status
            delete.return_value = response
            with self.assertRaises((RuntimeError, requests.HTTPError)):
                provider.delete_task("task-test")
