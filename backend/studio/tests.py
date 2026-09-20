from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections, connections
from django.test import TransactionTestCase, skipUnlessDBFeature

import io
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from . import media, provider, storage
from .billing import post
from .models import Asset, Chunk, Ledger, Project, Run, Wallet
from .services import generate, retry, split_duration
from .tasks import assemble, dispatch, finish, process_chunk


@override_settings(CREDITS_PER_SECOND=1, GENERATION_PROVIDER="mock", PROVIDER_MAX_SECONDS=30)
class StudioTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("alice", "", "test-only-long-password")
        self.other = get_user_model().objects.create_user("bob", "", "test-only-long-password")
        self.asset = Asset.objects.create(
            owner=self.user,
            kind="character",
            name="Character",
            key="1/assets/ref.jpg",
            mime="image/jpeg",
        )
        self.config = {
            "character": str(self.asset.id),
            "clothing": [],
            "location": None,
            "duration": 60,
            "action": "Walk slowly",
            "speech": "",
            "motions": [],
        }
        self.project = Project.objects.create(owner=self.user, config=self.config)
        post(self.user.id, "grant", 1000, "initial")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def start(self, key=None):
        return generate(self.user, self.project.id, "freeform", key or uuid.uuid4())

    def test_four_second_preview_creates_one_clip_and_reserves_four_seconds(self):
        response = self.client.patch(
            f"/api/projects/{self.project.pk}",
            {"revision": self.project.revision, "config": {**self.config, "duration": 4}},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        run = self.start()
        self.assertEqual(run.chunks.count(), 1)
        chunk = run.chunks.get()
        self.assertEqual((chunk.start, chunk.duration, chunk.cost), (0, 4, 4))
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual((wallet.available, wallet.held), (996, 4))
        with patch("studio.provider.storage.url", return_value="https://example.com/ref.jpg"):
            self.assertEqual(provider.payload(chunk)["duration"], 4)

    def test_reserve_and_idempotent_request(self):
        key = uuid.uuid4()
        run = self.start(key)
        again = self.start(key)
        self.assertEqual(run.id, again.id)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual((wallet.available, wallet.held, wallet.spent), (940, 60, 0))
        self.assertEqual(run.chunks.count(), 2)

    def test_insufficient_credit_rolls_back_whole_run(self):
        Wallet.objects.filter(user=self.user).update(available=40)
        with self.assertRaises(ValidationError):
            self.start()
        self.assertEqual(Run.objects.count(), 0)
        self.assertEqual(Chunk.objects.count(), 0)
        self.assertEqual(Wallet.objects.get(user=self.user).available, 40)

    def test_settle_once(self):
        chunk = self.start().chunks.first()
        finish(chunk.id, "ready", "output.mp4")
        finish(chunk.id, "ready", "output.mp4")
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual((wallet.available, wallet.held, wallet.spent), (940, 30, 30))
        self.assertEqual(Ledger.objects.filter(kind="settle").count(), 1)

    def test_failure_release_retry_and_success(self):
        chunk = self.start().chunks.first()
        finish(chunk.id, "error", message="Failed")
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual((wallet.available, wallet.held), (970, 30))
        retry(self.user, chunk.id)
        finish(chunk.id, "ready", "output.mp4")
        wallet.refresh_from_db()
        self.assertEqual((wallet.available, wallet.held, wallet.spent), (940, 30, 30))
        self.assertEqual(Chunk.objects.get(id=chunk.id).attempt, 2)

    def test_cannot_retry_ambiguous_or_ready(self):
        chunk = self.start().chunks.first()
        for state in ["review", "ready", "generating", "submitting", "queued"]:
            Chunk.objects.filter(id=chunk.id).update(state=state)
            with self.assertRaises(ValidationError):
                retry(self.user, chunk.id)

    def test_second_active_generation_blocked(self):
        self.start()
        with self.assertRaises(ValidationError):
            self.start()

    def test_snapshot_is_immutable(self):
        run = self.start()
        self.project.config["action"] = "Different action"
        self.project.save()
        run.refresh_from_db()
        self.assertEqual(run.snapshot["action"], "Walk slowly")

    def test_private_projects_and_assets(self):
        client = APIClient()
        client.force_authenticate(self.other)
        self.assertEqual(client.get(f"/api/projects/{self.project.id}").status_code, 404)
        result = client.get("/api/bootstrap").json()
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["projects"], [])
        self.assertEqual(
            client.post(
                f"/api/projects/{self.project.id}/generate",
                {"part": "freeform", "requestKey": str(uuid.uuid4())},
                format="json",
            ).status_code,
            404,
        )

    def test_cannot_attach_another_users_asset(self):
        foreign = Asset.objects.create(
            owner=self.other,
            kind="character",
            name="Private",
            key="private.jpg",
            mime="image/jpeg",
        )
        response = self.client.patch(
            f"/api/projects/{self.project.id}",
            {"revision": 1, "config": {**self.config, "character": str(foreign.id)}},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_optimistic_revision_prevents_lost_edits(self):
        payload = {"revision": 1, "config": self.config}
        self.assertEqual(
            self.client.patch(
                f"/api/projects/{self.project.id}", payload, format="json"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/projects/{self.project.id}", payload, format="json"
            ).status_code,
            409,
        )

    @override_settings(CREDITS_PER_SECOND=None)
    def test_pricing_must_be_configured(self):
        with self.assertRaises(ValidationError):
            self.start()

    def test_dialogue_is_allocated_without_repetition(self):
        from .services import dialogue_slice

        script = "One two three four five six seven eight nine ten eleven twelve"
        pieces = [dialogue_slice(script, start, 30, 90) for start in (0, 30, 60)]
        self.assertEqual(" ".join(pieces), script)
        self.assertEqual(len(set(pieces)), 3)

    def test_duration_splitting(self):
        for maximum in (15, 30):
            for duration in (4, 15, 30, 31, 32, 60, 90, 120):
                parts = split_duration(duration, maximum)
                self.assertEqual(sum(parts), duration)
                self.assertTrue(all(4 <= p <= maximum for p in parts))

    def test_motion_duration_limit(self):
        preset = Asset.objects.create(
            owner=self.user,
            kind="motion",
            name="Turn",
            key="turn.mp4",
            mime="video/mp4",
            duration=10,
        )
        response = self.client.patch(
            f"/api/projects/{self.project.id}",
            {
                "revision": 1,
                "config": {
                    **self.config,
                    "motions": [{"asset": str(preset.id), "duration": 30}],
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("studio.tasks.provider.payload", return_value={})
    @patch("studio.tasks.provider.submit", side_effect=TimeoutError)
    def test_unknown_submission_never_resubmitted(self, submit, payload):
        run = self.start()
        run.snapshot["provider"] = "byteplus"
        run.save()
        chunk = run.chunks.first()
        process_chunk(str(chunk.id))
        process_chunk(str(chunk.id))
        chunk.refresh_from_db()
        self.assertEqual(chunk.state, "review")
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(Wallet.objects.get(user=self.user).held, 60)

    @patch("studio.tasks.provider.payload", return_value={})
    @patch("studio.tasks.provider.submit", side_effect=provider.Rejected("Rejected"))
    def test_confirmed_rejection_releases_hold(self, submit, payload):
        run = self.start()
        run.snapshot["provider"] = "byteplus"
        run.save()
        chunk = run.chunks.first()
        process_chunk(str(chunk.id))
        chunk.refresh_from_db()
        self.assertEqual(chunk.state, "error")
        self.assertEqual(Wallet.objects.get(user=self.user).held, 30)

    @patch("studio.tasks.process_chunk.delay")
    def test_expired_submission_recovered_to_review(self, delay):
        chunk = self.start().chunks.first()
        Chunk.objects.filter(id=chunk.id).update(
            state="submitting", lease_until=timezone.now() - timedelta(seconds=1)
        )
        dispatch()
        chunk.refresh_from_db()
        self.assertEqual(chunk.state, "review")

    @patch("studio.tasks.provider.status", return_value=("running", None))
    @patch("studio.tasks.provider.submit")
    def test_poll_does_not_resubmit(self, submit, status):
        run = self.start()
        run.snapshot["provider"] = "byteplus"
        run.save()
        chunk = run.chunks.first()
        Chunk.objects.filter(id=chunk.id).update(state="generating", provider_id="real-id")
        process_chunk(str(chunk.id))
        submit.assert_not_called()
        status.assert_called_once_with("real-id")

    def test_csrf_required_for_login(self):
        client = Client(enforce_csrf_checks=True)
        result = client.post(
            "/api/login",
            {"username": "alice", "password": "test-only-long-password"},
            content_type="application/json",
        )
        self.assertEqual(result.status_code, 403)

    @override_settings(MFA_REQUIRED=False)
    def test_session_login_logout(self):
        client = Client(enforce_csrf_checks=True)
        token = client.get("/api/session").json()["csrfToken"]
        result = client.post(
            "/api/login",
            {"username": "alice", "password": "test-only-long-password"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.cookies[settings.SESSION_COOKIE_NAME]["httponly"])
        self.assertEqual(client.get("/api/bootstrap").status_code, 200)
        token = result.json()["csrfToken"]
        self.assertEqual(client.post("/api/logout", HTTP_X_CSRFTOKEN=token).status_code, 200)
        self.assertEqual(client.get("/api/bootstrap").status_code, 403)

    @override_settings(MFA_REQUIRED=True)
    def test_production_requires_mfa(self):
        client = Client()
        response = client.post(
            "/api/login",
            {"username": "alice", "password": "test-only-long-password"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_bad_login_throttled(self):
        client = Client()
        for _ in range(8):
            response = client.post(
                "/api/login",
                {"username": "nobody", "password": "wrong"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 401)
        response = client.post(
            "/api/login",
            {"username": "nobody", "password": "wrong"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)

    def test_image_upload_reencoded_and_private(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(MEDIA_ROOT=Path(folder), S3_BUCKET="", DEBUG=True),
        ):
            image = io.BytesIO()
            Image.new("RGB", (10, 10), "blue").save(image, "PNG")
            image.seek(0)
            image.name = "portrait.png"
            response = self.client.post(
                "/api/assets", {"file": image, "kind": "character"}, format="multipart"
            )
            self.assertEqual(response.status_code, 201)
            self.assertTrue(response.json()["url"].endswith("/preview"))
            with patch("studio.storage.fetch", wraps=storage.fetch) as fetch:
                self.assertEqual(self.client.get(response.json()["url"]).status_code, 200)
                self.assertEqual(fetch.call_count, 1)
            other = APIClient()
            other.force_authenticate(self.other)
            self.assertEqual(other.get(response.json()["url"]).status_code, 404)

    def test_nonimage_rejected(self):
        fake = io.BytesIO(b'<svg onload="alert(1)"></svg>')
        fake.name = "fake.jpg"
        self.assertEqual(
            self.client.post(
                "/api/assets", {"file": fake, "kind": "character"}, format="multipart"
            ).status_code,
            400,
        )

    def test_export_requires_both_parts(self):
        self.assertEqual(
            self.client.post(f"/api/projects/{self.project.id}/export").status_code, 400
        )

    def test_export_idempotent_and_no_new_credit_charge(self):
        for part in ("freeform", "motion"):
            run = Run.objects.create(
                project=self.project,
                part=part,
                snapshot=self.config,
                request_key=uuid.uuid4(),
                state="ready",
            )
            Chunk.objects.create(
                run=run,
                position=0,
                start=0 if part == "freeform" else 60,
                duration=30,
                state="ready",
                output_key="output.mp4",
                cost=0,
            )
        first = self.client.post(f"/api/projects/{self.project.id}/export")
        second = self.client.post(f"/api/projects/{self.project.id}/export")
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Wallet.objects.get(user=self.user).available, 1000)

    def test_mock_generation_and_ffmpeg_assembly(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(MEDIA_ROOT=Path(folder), S3_BUCKET="", DEBUG=True),
        ):
            run = self.start()
            # Keep the media integration test short; accounting still uses original reservations.
            run.chunks.update(duration=1)
            for chunk in run.chunks.all():
                process_chunk(str(chunk.id))
            assemble(str(run.id))
            run.refresh_from_db()
            self.assertEqual(run.state, "ready")
            output = Path(folder) / run.output_key
            self.assertTrue(output.is_file())
            info = media.probe(output)
            self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=0.2)
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (720, 1280))

    def test_mfa_code_cannot_be_replayed(self):
        import pyotp
        from cryptography.fernet import Fernet

        from .models import MfaDevice

        key = Fernet.generate_key()
        secret = pyotp.random_base32()
        MfaDevice.objects.create(
            user=self.user,
            encrypted_secret=Fernet(key).encrypt(secret.encode()).decode(),
        )
        with override_settings(MFA_REQUIRED=True, MFA_ENCRYPTION_KEY=key.decode()):
            data = {
                "username": "alice",
                "password": "test-only-long-password",
                "code": pyotp.TOTP(secret).now(),
            }
            self.assertEqual(
                Client().post("/api/login", data, content_type="application/json").status_code,
                200,
            )
            self.assertEqual(
                Client().post("/api/login", data, content_type="application/json").status_code,
                401,
            )

    @override_settings(OUTPUT_HOSTS=["media.example.com"])
    def test_output_download_rejects_private_dns(self):
        with patch(
            "studio.provider.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(ValueError):
                provider.download("https://media.example.com/output.mp4", "/tmp/never-written.mp4")

    @override_settings(OUTPUT_HOSTS=["media.example.com"])
    def test_output_download_rejects_other_hosts(self):
        for url in [
            "http://media.example.com/x",
            "https://evil.example/x",
            "https://media.example.com.evil.example/x",
            "https://user:password@media.example.com/x",
        ]:
            with self.assertRaises(ValueError):
                provider.download(url, "/tmp/never-written.mp4")

    def test_new_run_hides_stale_full_export(self):
        first = self.start()
        second = Run.objects.create(
            project=self.project,
            part="motion",
            snapshot=self.config,
            request_key=uuid.uuid4(),
            state="ready",
        )
        old = Run.objects.create(
            project=self.project,
            part="full",
            snapshot={"sources": [str(first.id), str(second.id)]},
            request_key=uuid.uuid4(),
            state="ready",
            output_key="old.mp4",
        )
        Run.objects.create(
            project=self.project,
            part="freeform",
            snapshot=self.config,
            request_key=uuid.uuid4(),
        )
        data = self.client.get(f"/api/projects/{self.project.id}").json()
        self.assertNotIn(str(old.id), [r["id"] for r in data["runs"]])


@override_settings(CREDITS_PER_SECOND=1, GENERATION_PROVIDER="mock", PROVIDER_MAX_SECONDS=30)
@skipUnlessDBFeature("has_select_for_update")
class PostgresConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("concurrency")
        asset = Asset.objects.create(
            owner=self.user,
            kind="character",
            name="Character",
            key="ref.jpg",
            mime="image/jpeg",
        )
        config = {
            "character": str(asset.id),
            "duration": 60,
            "action": "Walk",
            "clothing": [],
            "location": None,
            "speech": "",
            "motions": [],
        }
        self.projects = [Project.objects.create(owner=self.user, config=config) for _ in range(2)]
        post(self.user.id, "grant", 100, "concurrency-initial")

    def launch(self, project_id, request_key):
        close_old_connections()
        try:
            return str(generate(self.user, project_id, "freeform", request_key).id)
        except ValidationError:
            return "insufficient"
        finally:
            connections.close_all()

    def test_parallel_projects_cannot_overspend(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: self.launch(p.id, uuid.uuid4()), self.projects))
        self.assertEqual(results.count("insufficient"), 1)
        self.assertEqual(Run.objects.count(), 1)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual((wallet.available, wallet.held), (40, 60))

    def test_parallel_same_request_creates_one_run(self):
        key = uuid.uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.launch(self.projects[0].id, key), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(Run.objects.count(), 1)

@override_settings(S3_BUCKET="private-test")
class PrivateMediaTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("media-owner")
        self.other = get_user_model().objects.create_user("media-other")
        self.key = f"{self.user.pk}/assets/image.jpg"
        Asset.objects.create(owner=self.user, kind="character", name="Image", key=self.key, mime="image/jpeg")
        self.client = APIClient()

    def test_browser_urls_hide_storage_but_provider_urls_remain_signed(self):
        from . import storage
        with patch("studio.storage.client") as s3:
            self.assertEqual(storage.url(self.key), f"/api/media/{self.key}")
            s3.assert_not_called()
            storage.url(self.key, provider=True)
            s3.return_value.generate_presigned_url.assert_called_once_with(
                "get_object", Params={"Bucket": "private-test", "Key": self.key}, ExpiresIn=3600
            )

    def test_anonymous_other_user_and_unknown_key_never_access_s3(self):
        with patch("studio.storage.client") as s3:
            self.assertEqual(self.client.get(f"/api/media/{self.key}").status_code, 403)
            self.client.force_authenticate(self.other)
            self.assertEqual(self.client.get(f"/api/media/{self.key}").status_code, 404)
            self.client.force_authenticate(self.user)
            self.assertEqual(self.client.get(f"/api/media/{self.user.pk}/unknown").status_code, 404)
            s3.assert_not_called()

    def test_range_stream_and_head_hide_upstream_headers(self):
        from botocore.response import StreamingBody
        self.client.force_authenticate(self.user)
        with patch("studio.storage.client") as s3:
            body = StreamingBody(io.BytesIO(b"abc"), 3)
            s3.return_value.get_object.return_value = {
                "Body": body, "ContentLength": 3, "ContentType": "video/mp4", "ContentRange": "bytes 2-4/10"
            }
            response = self.client.get(f"/api/media/{self.key}", HTTP_RANGE="bytes=2-4")
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b"".join(response.streaming_content), b"abc")
            self.assertEqual(response["Content-Range"], "bytes 2-4/10")
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertNotIn("Location", response)
            s3.return_value.get_object.assert_called_once_with(Bucket="private-test", Key=self.key, Range="bytes=2-4")
            s3.return_value.head_object.return_value = {"ContentLength": 10, "ContentType": "video/mp4"}
            self.assertEqual(self.client.head(f"/api/media/{self.key}").status_code, 200)
            self.assertEqual(s3.return_value.get_object.call_count, 1)
            self.assertEqual(self.client.get(f"/api/media/{self.key}", HTTP_RANGE="bytes=1-2,4-5").status_code, 416)
