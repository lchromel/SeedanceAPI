# Video Studio on Railway

The new application lives in `backend/` and `frontend/`. `web_app.py` remains a standalone legacy prototype. Its JSON material library and single-account SQLite authentication are **not** automatically imported into the new application.

## Services

Create four services from the same repository, with repository root as build context:

| Service | Config file | Public domain |
|---|---|---|
| Web | `/railway.json` | Yes, HTTPS only |
| Generation worker | `/deploy/worker.railway.json` | No |
| Media worker | `/deploy/media.railway.json` | No |
| Scheduler | `/deploy/beat.railway.json` | No; exactly one replica |

Add Railway PostgreSQL and Redis. Reference their **private** connection URLs from all four services. All durable application state resides in PostgreSQL and private S3 storage; Railway volumes are not required. Run migrations through the web pre-deploy command before starting workers for the first time.

## Shared variables

- `STUDIO_DEBUG=0`
- `DJANGO_SECRET_KEY`: cryptographically random, at least 50 characters. Same across services.
- `MFA_ENCRYPTION_KEY`: Fernet key, generate with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` in a trusted terminal. Same across services; back up securely outside the DB.
- `DATABASE_URL`: Railway PostgreSQL private connection URL.
- `REDIS_URL`: Railway Redis private connection URL.
- `ALLOWED_HOSTS`: exact web hostname (comma-separated if multiple). The Railway healthcheck hostname is added by production settings automatically.
- `CSRF_TRUSTED_ORIGINS`: exact `https://` web origin.
- `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`: private S3-compatible bucket credentials. Disable public access and public ACLs. Limit credentials to the application bucket. Configure provider-supported at-rest encryption and lifecycle rules for abandoned objects.
- `GENERATION_PROVIDER=byteplus`: direct BytePlus API; no aggregators or implicit fallback providers.
- `ARK_API_KEY`, `ARK_MODEL`: verified key and enabled video model/endpoint.
- `PROVIDER_MAX_SECONDS`: verified maximum of that model (default 30). Shorter limits produce additional provider clips, preserving the total duration.
- `PROVIDER_OUTPUT_HOSTS`: exact trusted output hostnames or domain suffixes documented by the provider. Required before paid generation; redirects are refused. Do not use broad suffixes such as `com`.
- `CREDITS_PER_SECOND`: integer internal tariff chosen by the administrator. Unset means generation is disabled. There is no automatic starting balance or invented production tariff.

Never set `STUDIO_DEBUG=1` or `GENERATION_PROVIDER=mock` in production. Keep services behind Railway's edge; the application trusts its HTTPS forwarded-protocol header. No access logs are emitted by Gunicorn. Celery warning logs carry IDs, not task payloads (tasks accept IDs only).

## Accounts and credits

There is no public signup or public admin panel. In a trusted terminal attached to the web service:

```
python manage.py create_member USERNAME
python manage.py enroll_mfa USERNAME
python manage.py grant_credits USERNAME 2000 --reference INITIAL-ALLOCATION-001
```

`create_member` prompts privately for a password, validates it, and creates a non-staff member. Enroll each member's authenticator. No plaintext passwords are stored. Credit allocation references are idempotent; 2000 above is an example, not an applied allocation.

## Job recovery and accounting

The scheduler discovers committed job rows every 15 seconds. Redis is transport, not the authoritative job state. Row locks and expiring leases prevent concurrent duplicate submissions. A submission with unknown outcome goes to `review`, retaining its credit reservation. It is NEVER blindly resubmitted.

After examining the provider dashboard, an operator can attach the actual task ID:

```
python manage.py reconcile_chunk CHUNK_UUID --provider-task-id TASK_ID
```

Or confirm the provider did not create a task:

```
python manage.py reconcile_chunk CHUNK_UUID --confirmed-not-created
```

Credit reservation, settlement and release use database transactions and unique ledger keys. Users can retry only confirmed failures. Successful clips are downloaded to private storage before settlement. Final assembly runs separately. An assembly error keeps the completed clips and retries assembly without generation charges.

Generation settings are snapshotted per run. Freeform and motion are separate runs. Export assembles their latest completed versions after checking compatible character/clothing/location/timing. Old runs and files remain retained. Retention/deletion policy must be chosen before sustained production use.

## Confidentiality and operations

The application does not log prompts, uploaded media or provider response bodies. Uploaded images are decoded/re-encoded with metadata removed. Motion uploads have metadata stripped. Browser session cookies are HttpOnly/Secure/SameSite Strict in production; mutations require CSRF protection. MFA secrets are encrypted separately from the database. Login attempts are rate-limited using persistent database rows. Direct-peer IP limiting may aggregate users behind Railway's proxy; username limits remain independent.

Private asset URLs expire after 5 minutes for the browser and 1 hour when supplied to the generation provider; these are bearer links, not end-to-end encryption. BytePlus still receives selected references and prompts. Confirm account-specific retention/training/review terms before uploading sensitive content. Railway/S3 operator access is not technically eliminated.

Enable PostgreSQL backups and bucket versioning/lifecycle according to your chosen retention policy; test restoration. Store recovery keys separately. MFA recovery is an operator procedure, not a public reset endpoint. File processing is bounded and disallows network protocols in FFmpeg, but containers/FFmpeg must remain patched. A single DB is not a high-availability deployment by itself.

## Local development

Python 3.11/3.12, Node 22, FFmpeg + ffprobe are required.

```
python3.11 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock
npm --prefix frontend ci
npm --prefix frontend run build
export STUDIO_DEBUG=1 GENERATION_PROVIDER=mock CREDITS_PER_SECOND=1
.venv/bin/python backend/manage.py migrate
.venv/bin/python backend/manage.py createsuperuser
.venv/bin/python backend/manage.py grant_credits USERNAME 2000 --reference local-test
.venv/bin/python backend/manage.py collectstatic --noinput
.venv/bin/python backend/manage.py runserver 127.0.0.1:8081
```

Simulation is clearly labeled in the UI and creates neutral gray video clips with silent audio. It does not contact any model provider. To execute pending simulation jobs without Redis:

```
STUDIO_DEBUG=1 GENERATION_PROVIDER=mock .venv/bin/python backend/manage.py run_local_jobs
```

For live local editing, `npm --prefix frontend run dev` proxies API requests to port 8081. Add the printed Vite origin to `CSRF_TRUSTED_ORIGINS` for local POST requests. Production serves compiled frontend and API from one HTTPS origin.

## Verification

```
STUDIO_DEBUG=1 .venv/bin/python backend/manage.py test studio
npm --prefix frontend run build
```

For PostgreSQL locking tests, provide a disposable `DATABASE_URL` whose user can create test databases. SQLite is allowed only for local development and cannot validate row-lock concurrency. Real provider calls and production deployment are intentionally separate from local checks.

Reference deployment docs: [Railway healthchecks](https://docs.railway.com/deployments/healthchecks), [pre-deploy commands](https://docs.railway.com/deployments/pre-deploy-command).

Dialogue is distributed by word count across clips to avoid repeating the entire script. This is a basic allocation, not a semantic storyboard or lip-sync guarantee. Cross-clip visual continuity and actual model duration/reference support still require a real-provider acceptance test with non-sensitive material.

For an interactive local preview, run `run_local_jobs --watch` in a second terminal. It continuously processes simulation jobs and refuses to run unless both debug mode and the mock provider are enabled. Production uses the separate Celery workers and scheduler instead.

See [VERIFICATION.md](VERIFICATION.md) for executed checks and remaining live-infrastructure checks.


### BytePlus character catalog

Character library syncs image assets from the configured BytePlus project (AIGC and
LivenessFace groups), using server-only `BYTEPLUS_ACCESS_KEY_ID`,
`BYTEPLUS_SECRET_ACCESS_KEY`, and `BYTEPLUS_ASSET_PROJECT` (default: `default`).
The credentials require ListAssets and GetAsset permissions. Add characters in
BytePlus Assets, then use Refresh in Yellow. Clothing, locations and motion
references remain private uploads.

Catalog records are scoped to the authenticated user; repeated syncs preserve
selection IDs. Only Active characters are selectable. Missing assets are marked
unavailable; a failed sync preserves the previous catalog. Previews are fetched
through authenticated Yellow endpoints, restricted to the BytePlus asset storage
host, bounded to 20 MB and re-encoded as JPEG. Provider URLs and credentials never
reach the browser. Generation sends the original `asset://` ID.


### AI reference descriptions and freeform enhancement

Clothing and location uploads require a category. The server reads the sanitized
private image, reduces it to 1024 px and sends it inline to the vision model;
DeepSeek then writes the English name and reference description. Failed analysis
keeps the upload and can be retried under Reference details. Existing uploads need
a category and analysis there before being used in a new generation.

Both models use the existing server-only `ARK_API_KEY`. Optional overrides:
`REFERENCE_VISION_ENDPOINT_ID` and `DEEPSEEK_ENDPOINT_ID` (the legacy
`BYTEPLUS_DEEPSEEK_ENDPOINT_ID` is also supported). Defaults match the original
app: `seed-2-0-lite-260228` and `deepseek-v4-pro-260425`. These calls use the
provider account's inference billing; they do not launch video-generation jobs.

Improve with DeepSeek uses the current unsaved scene and selected reference
descriptions. It replaces the scene direction only; dialogue stays untouched.
Full prompt shows the auto-generated reference instructions plus the scene.
Image order is always character, optional location, then selected clothing order.
New generation snapshots freeze this order and the reference descriptions.

Railway: set the service Pre-deploy Command to `python manage.py migrate --noinput`
in service settings. The production service has this configured. Do not rely on
an unconfigured railway.json being picked up automatically by a new service.
