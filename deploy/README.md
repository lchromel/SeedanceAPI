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
- `CREDITS_ENABLED=0`: default while building the platform. Image/video generation
  does not require pricing or a positive credit balance; no internal credit ledger
  entries are created for these new jobs. Credit amounts and history are hidden
  in the UI. Model-provider charges still apply.
- `CREDITS_PER_SECOND`: video tariff used only with `CREDITS_ENABLED=1`. Without
  a configured tariff, enabled credit billing blocks new video generations.

Never set `STUDIO_DEBUG=1` or `GENERATION_PROVIDER=mock` in production. Keep services behind Railway's edge; the application trusts its HTTPS forwarded-protocol header. No access logs are emitted by Gunicorn. Celery warning logs carry IDs, not task payloads (tasks accept IDs only).

## Accounts and credits

There is no public signup or public admin panel. In a trusted terminal attached to the web service:

```
python manage.py create_member USERNAME
python manage.py enroll_mfa USERNAME
python manage.py grant_credits USERNAME 2000 --reference INITIAL-ALLOCATION-001
```

`create_member` prompts privately for a password, validates it, and creates a non-staff member. Enroll each member's authenticator. No plaintext passwords are stored. Credit allocation references are idempotent; 2000 above is an example, not an applied allocation.

Credit allocation is optional while `CREDITS_ENABLED=0`. To restore credit billing,
set `CREDITS_ENABLED=1`, `CREDITS_PER_SECOND` and `CREDITS_PER_IMAGE` consistently
on web, generation worker and scheduler, then restart them. Existing wallet
balances and history are retained. Reservations from before the pause finish
using their stored costs; new jobs and retries started during the pause cost zero.
Jobs created during the pause are not charged retroactively when billing resumes.

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

After a new BytePlus clip is downloaded, probed, uploaded to private storage and
committed as ready, the same transaction records a pending provider cleanup.
The existing Celery beat dispatcher (every 15 seconds) sends these to a separate
cleanup task, which calls `DELETE /contents/generations/tasks/{id}`. Run database
migrations before restarting web/worker/beat when deploying this change.
Cleanup retries failures with exponential delays from 60 seconds up to one hour;
expired worker leases are recovered automatically. HTTP 404 is treated as already
deleted. Cleanup failures never change clip readiness or billing. Inspect the
Chunk fields `cleanup_state`, `cleanup_attempts`, `cleanup_next_attempt`,
`cleanup_error`, and `provider_deleted_at` for operational status; persistent
authentication errors require correcting the provider credentials.
Previously completed clips are not automatically enrolled by the migration.
This deletes the provider task record; BytePlus does not document this endpoint
as a guarantee of physical erasure from its CDN, backups or internal logs.

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
export STUDIO_DEBUG=1 GENERATION_PROVIDER=mock CREDITS_ENABLED=0
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

Clothing and location uploads automatically detect a category. The server reads the sanitized
private image, reduces it to 1024 px and sends it inline to the vision model;
DeepSeek then writes the English name and reference description. Failed analysis
keeps the upload and can be retried via the pencil icon. This editor also allows
manual name, category and description corrections without an AI request. Existing
unanalyzed uploads can use Auto-detect again there before a new generation.
Location categories describe place types (Apartment, Hotel, Villa, etc.); uncertain
images use Other rather than guessing a specific place type.

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

### Image studio — Seedream 5 Pro

The sidebar switches between Video and Images. Images accepts up to ten JPG, PNG
or WebP references, 30 MB each, in attachment order (Image 1, Image 2, etc.). Uploads
are sanitized and stored privately. The image draft remains available when
switching sections. Generation history and completed files survive reloads.

Improve with DeepSeek sends all attached images to the vision model first, then
uses its observations to write a full English still-image prompt. Optional notes
can be in any language. The returned prompt stays editable and supports Undo;
an answer for an outdated draft never overwrites newer edits. Improvement does
not start generation. It uses the existing server-only ARK key and vision/DeepSeek
model overrides; these inference calls use the provider's billing.

Set on web, generation worker and scheduler:

- `CREDITS_PER_IMAGE`: required only with `CREDITS_ENABLED=1`; a positive integer
  internal tariff for one generated image. With the default `CREDITS_ENABLED=0`,
  image generation is independent of this variable and the user's credit balance.
  There is no default production tariff. The same internal tariff applies to all
  offered sizes/reference counts; this is not a claim about provider pricing.
- `SEEDREAM_ENDPOINT_ID`: optional Pro endpoint override; default model is
  `dola-seedream-5-0-pro-260628`. `BYTEPLUS_SEEDREAM_ENDPOINT_ID` is also supported.
- Existing `ARK_API_KEY`, private S3 settings and `PROVIDER_OUTPUT_HOSTS` must be
  configured. The output allowlist must include the actual image result hosts.

Apply migrations before restarting web, worker and beat. The existing Celery
generation worker and scheduler also process image jobs; no additional service
is needed. The adapter calls `/images/generations`, creates one image per request,
and offers 1K, 1.5K and 2K with five aspect ratios, sent as explicit pixel sizes.
There is no batch/streaming parameter or fallback to a different image model.

When `CREDITS_ENABLED=1`, credits are reserved atomically with idempotent job creation, settled after the
image is stored privately, and released on confirmed rejection. A timeout during
paid submission leaves the job in `review`, retaining its reservation. Never
blindly resubmit it. After checking the provider dashboard, either release a
confirmed absent result or resume saving a confirmed generated result:

```
python manage.py reconcile_image JOB_UUID --confirmed-not-created
python manage.py reconcile_image JOB_UUID --output-url VERIFIED_PROVIDER_IMAGE_URL
```

An interrupted download retries from the saved provider URL without another
generation request. If that URL expires, an administrator must investigate the
provider result before changing the reservation. The API never exposes provider
result URLs to the browser; previews/downloads use authenticated private media.

For local simulation, keep `CREDITS_ENABLED=0` and use the existing
`run_local_jobs` command. Simulation produces a labeled neutral JPEG placeholder
and makes no generation request. Real DeepSeek assistance remains disabled
without an ARK key, including in simulation mode.

Image enhancement accepts equivalent reference wording such as Image1, Image #1,
photo one or the first image. Unknown image numbers are still rejected. If a
reference is omitted or misnumbered, DeepSeek gets one bounded text-only repair
attempt with the original request and the same verified observations. Vision is
not repeated; that repair adds one inference call only when needed. A failed
repair preserves the user's draft.

The image vision stage also normalizes explicit numeric/string image labels,
reordered entries and JSON mappings/fences. Missing, duplicate, conflicting or
invalid descriptions are not guessed or silently dropped. Only those sources
are analyzed again, each in an isolated one-image request; the server assigns
their original image numbers. Ambiguous aggregate numbering triggers isolated
analysis of every source. Recovery uses at most four concurrent vision calls,
one per missing source, within the enhancement's 100-second budget. Writing and
its optional repair use the remaining time. This can add inference calls when
an aggregate vision response is incomplete. Genuine analysis failures preserve
the draft and identify the affected image. Logs contain recovery counts only,
not images, descriptions or provider responses.

API reference: [BytePlus image generation](https://docs.byteplus.com/en/docs/modelark/image-generation-api),
[Seedream 5 Pro examples](https://docs.byteplus.com/en/docs/modelark/seedream-5-0-pro).

### Image storage switch and temporary retention

Set the same variables on web and generation worker (and the scheduler for
metadata cleanup), then deploy with migrations:

```
SAVE_IMAGES_TO_S3=0
IMAGE_REFERENCE_TTL_SECONDS=3600
IMAGE_DOWNLOAD_TTL_SECONDS=600
```

- `SAVE_IMAGES_TO_S3=1` is the default: new Image studio references/previews and
  generated results retain the existing private S3 behavior.
- `SAVE_IMAGES_TO_S3=0` routes new Image studio references/previews and generated
  results exclusively to a shared, private Redis buffer in Railway. No new image
  bytes for this section are written to S3, including when a bucket is configured.
  There is no fallback to S3 if the temporary buffer is unavailable.
- `IMAGE_TEMP_REDIS_URL` selects a dedicated private Redis instance for temporary
  images. If unset, `REDIS_URL` is used. Both web and worker must use the same URL.
  Redis holds the bytes; PostgreSQL stores IDs, deadlines and generation metadata.
  Worker-local processing files use a temporary directory removed at task exit.
- Unused source references expire after `IMAGE_REFERENCE_TTL_SECONDS` (default one
  hour). Each queued generation gets independent reference copies with the same
  TTL. A very long queue can therefore expire safely before any provider request.
- After provider submission returns, source references/previews and that job's
  input copies are deleted. Mock jobs delete them after completion. Confirmed
  failures and uncertain submissions also release these temporary bytes. Other
  already-queued jobs keep their own copies.
- A completed result expires after `IMAGE_DOWNLOAD_TTL_SECONDS` (default ten
  minutes). Redis TTL deletes the bytes independently of Celery. The authenticated
  media endpoint refuses expired downloads; the UI shows the download deadline
  and then Image deleted. Beat also removes expired reference records and clears
  expired output keys. IDs, prompt text and generation history remain in the DB.
- Storage choice and result lifetime are snapshotted when a generation is created.
  Changing the switch does not move/delete earlier S3 files or change queued jobs'
  result retention. Video outputs and look-library assets keep their existing
  storage behavior; production still needs its S3 configuration for those features.

This uses shared Redis because the web service's filesystem is not shared with
the generation worker; do not mount one Railway volume on several services.
[Railway volume reference](https://docs.railway.com/volumes/reference).

TTL/DEL revoke the application's live copies. For a genuinely non-persistent
temporary buffer, use a dedicated Redis configured without RDB snapshots, AOF
or volume backups (for example, a dedicated server started with `--save ""`
and `--appendonly no`). Do not change persistence of the application's existing
broker without considering its other uses. Deleting a Redis key does not purge
older snapshots/backups, and does not control copies held by BytePlus or already
downloaded by the user. [Redis persistence](https://redis.io/docs/latest/management/persistence/).

### Photo series — per-original replacement generation

Images has Create image, Photo series, Makeup & hair and Wardrobe modes. Photo series separates:

1. Up to 50 original photographs, each with its own editable full English prompt
   and optional individual wishes.
2. Shared optional references for the new model, clothing, makeup, hairstyle and location,
   plus general wishes for the series.

An empty replacement slot preserves that aspect from each original. Each prompt
sees only its own original as Image 1, followed by the selected replacements in
model/clothing/makeup/hairstyle/location order, then captioned extra references. A reference reused for multiple roles is
sent once with both roles assigned. Other originals never become references for
that photograph's generation. The initial aspect-ratio choice preserves each
original's proportions; a fixed ratio can be selected for the whole series.

Prepare individual prompts calls the vision model and DeepSeek separately for
each photograph, with three bounded concurrent workers by default (selectable
from one to four). It reuses the existing response/reference validation and one
bounded text-repair attempt per photograph. Stop queued prompts lets in-flight
calls finish without starting more. Users can retry one failed prompt or only
failed rows, keeping successful drafts, or write full English prompts manually. This phase
does not launch image generation; provider inference charges still apply. Shared
references are currently analyzed with each original. Updating replacements,
wishes, notes, ratio or resolution marks the relevant drafts for review/update
before generation. The complete writer instructions are versioned in
`backend/studio/prompts/seedream_replacement.md` and copied into the Docker image.

Reference validation recognizes explicit grouped numeric citations such as
`Images 1 and 2`, `Image 1 and 2`, comma-separated lists and ascending ranges
(`Images 1–3`), in addition to the supported individual labels/ordinals. Unknown
and zero-based IDs still fail validation. Before writing, embedded analysis
citations are rebound to the actual generation order; an excluded location is
named as prose. Shared-location observations lose their single-input analysis
label to avoid confusing the original photograph with the location.

If the writer omits a reference whose English role is already known from the
server's replacement plan, the application adds that exact role assignment to the
full prompt without another inference call. Missing extra-reference roles and
unknown numbers still receive one bounded AI correction using the complete
instructions in `backend/studio/prompts/seedream_reference_repair.md`. Failed
correction reports the missing/unexpected Image numbers. Warning diagnostics log
only those number sets and attempt counts, without prompts, observations, image
IDs, filenames or credentials.

All editing modes accept captioned extras (`extras: [{asset, label}]`), with a
combined limit of ten unique images including that original. Captions are required
and every reference is owner/availability checked. Exact labels are assigned
after deduplication and included in DeepSeek's context. Updating captions or fixed
identity values invalidates prepared drafts for review/update.

Fixed face details are shared between editing modes and saved in localStorage
under the current account ID. The UI selects Blue by default; the color list also
contains Green, Brown, Hazel, Gray and Amber. The request includes
`identity: {eyeColor, details}`. Details are entered in English for use in manual
as well as enhanced prompts. The server includes these values in DeepSeek's
context, appends an English identity footer to generation prompts and stores
`identity_lock` in every job snapshot. Footer replacement is idempotent and honors
the final prompt length limit. Existing API clients may omit identity constraints.

The same replacement endpoints accept `workflow: "makeup"` or `"wardrobe"`
(default `"replacement"`). Focused modes accept one original. Makeup requires a
makeup and/or hairstyle reference; Wardrobe requires a clothing reference or
captioned garment extras. Roles from other workflows are rejected. Makeup retains
the original facial anatomy, expression, skin tone, clothing and background;
cosmetics/hair donors do not replace identity. Wardrobe retains facial identity,
body proportions, pose and the layout of an existing multi-view sheet, applying
the outfit consistently across its views. Vision receives role-specific
observation tasks. Both workflows append their edit scope even for manually
written prompts and appear separately in history. Actual visual fidelity still
depends on the generation model.

With a location reference, vision receives separate observation tasks for the
original's camera/crop/subject geometry and the donor environment's layout,
architecture, materials, colors, depth and light. These tasks survive single-image
analysis recovery. DeepSeek is instructed to describe the observed environment as
it would appear around this original's subject and camera, including the portion
visible in the crop and plausible support surfaces, rather than only naming the
location reference. Unseen geometry remains unspecified.

Use location for description only is unchecked by default. Enable it in Photo
series after uploading a location (`locationDescriptionOnly: true`). Before the
individual prompt queue starts, the UI creates one shared location identity via
`POST /api/images/replacements/location-profile` with `{asset}`. This returns
`{asset, description, proof}`. The description fixes the observed palette,
furniture designs/materials, bedding, walls, lights and recognizable details,
independent of any original photograph's camera or pose. It is displayed in
Shared location description and reused for batch preparation, single-photo
enhancement and failed-row retries while the same location is selected. The
client refreshes the cached identity after 23 hours; changing its proof invalidates
previously prepared drafts for update.

Individual prompt and generation requests pass the same object as
`locationProfile`. Its signed proof is valid for 24 hours and binds the owner,
location image, vision model/version and exact description. Modified, incomplete,
foreign or mismatched profiles are rejected before AI requests. Existing API
clients may omit the optional profile and retain per-photo behavior. DeepSeek
receives the shared identity as the authoritative source for stable room
appearance, while each photo's separate transfer plan supplies camera, crop,
body contact and object correspondence. Different crops need not show every room
feature; visible materials and furniture retain their shared identity. The UI also
uses this shared identity when the location image remains attached to generation.

Vision
examines both the original and location with an image edge up to 1280 px and
requests a joint scene-transfer plan within 2400 characters: Original support,
Same-object match, Target staging and Appearance. The plan identifies furniture
supporting the body, its visible count, orientation and contact, then matches it
to the donor furniture. A hotel bed restyles the existing occupied bed rather
than adding a background bed. The original supplies the support geometry and
camera; the donor supplies appearance and compatible surrounding details. Recovery
of a missing location observation also receives both actual images, while other
reference recovery remains isolated. DeepSeek then writes the scene
in self-contained English prose adapted to that original's camera, crop, pose and
subject placement. The location image is removed from generation references and
provider input keys; remaining image labels, observations, source roles and extra
captions are renumbered to match the actual generation inputs. The combined
analysis limit remains ten images, including original and location. A location
assigned to another role or also used as the original is rejected in this mode.

Prompt preparation returns `locationDescription` and `locationPreparation`, which
the client includes for each original in the generation request. The latter is a
server-signed proof bound to that original, description, references, wishes,
identity/output settings and notes, valid for 24 hours. Generating without current
preparation, using stale image labels, or changing the signed description is
rejected before creating jobs. The draft can still be edited after preparation.
The proof includes the geometry-policy version, so earlier preparation must be
refreshed after a policy change. Geometry and support-object rules stay in the
internal analysis/writer instructions. The final scene text describes the actual
occupied furniture and surroundings, without an appended generic `Location geometry`
block. Older control blocks are removed when preparing a new prompt. The job stores `location_geometry_version`; explicit retries
retain the stored version and prompt. Existing results require a newly prepared
prompt in a new draft to use the updated scene-transfer analysis.
Observed location details are expandable under the individual prompt; its final
scene description is part of the full prompt. This mode requires AI preparation,
including when other modes allow manual prompts without an assistance key.

The saved job keeps the description and mode choice. Explicit regeneration needs
only its actual generation references and stored prompt, with no location upload
or analysis call. The signed shared identity is stored as `location_profile` in
each job and retained on regeneration. Profiles add no server-side image copy or
schema migration; the browser keeps its active profile in memory. Temporary location uploads still join input cleanup after the
first completed submission, despite being absent from generation inputs. Existing
S3 retention behavior is unchanged. The observation cache below requires migration 0009.

### Reusing visual analysis

Successful image observations are stored in `ImageObservation` (migration
`0009_imageobservation`) and reused for the same owned asset, vision model,
analysis focus, resolution and policy version. Scene-transfer observations also
include the specific original/location pair in their key. Shared model, clothing,
makeup and extra-reference observations are reused across originals; a rewrite or
retry does not reread already analyzed sources. The same cache also serves the
shared location identity stage. The cache stores text, with no additional image
copy, and is cascade-deleted with its source Asset, including temporary cleanup.

Atomic database leases coordinate web workers without holding a transaction over
provider HTTP calls. Another request reuses the result instead of sending the same
analysis concurrently. Partial successes are saved before an error is returned;
the next request sends only missing sources. A location transfer still receives
its actual original image as a geometric anchor. Internal image-number citations
are neutralized in cached observations to keep reuse independent of source order.

Missing-observation recovery runs at most two calls concurrently per request.
Recovery has up to 25 seconds and one compact retry for transient timeout,
rate-limit or incomplete-output errors, within the original request time budget.
Location recovery continues to see both images. Diagnostics record only error
categories and reference numbers; provider bodies and image data are not logged.
Image generation submission is not automatically retried by this mechanism.

### Structured photographic prompts

The UI opts into `structuredPrompt: true` for AI prompt writing in Create image,
Photo series, Makeup & hair and Wardrobe. Eight required sections are returned:
FORMAT + MEDIUM, SUBJECT + AGE, CAMERA + FRAMING, SCENE + SETTING, MICRO-DETAILS,
EXPRESSION + ACTION, LIGHTING and NEGATIVE CUES. The complete formatting rules are
in `backend/studio/prompts/image_prompt_format.md`, combined with the mode's writer
instructions. Harmless heading Markdown/case is normalized; missing sections get
the same single bounded correction used for reference repair. API clients may
omit the flag to retain plain prompt behavior, and manual drafts remain editable.

Scene text must be concrete and source-grounded. Camera brands, exact ages,
distances and focal lengths are included when supplied, rather than inferred
capture metadata. Fixed iris color and face features are integrated under
`SUBJECT + AGE`; the tattoo setting is integrated under `MICRO-DETAILS`. Structured prompts
do not receive separate application-settings or generic geometry footers. NEGATIVE
CUES is concise and task-specific. All eight sections and controls still fit the
6000-character limit. Existing results keep their saved prompts; prepare a new
prompt to use this structure.

Keep tattoos is unchecked by default (`keepTattoos: false` on both replacement
endpoints). The checked state preserves observed tattoos from the selected model,
or from the original if no model was selected; vision is asked to observe their
design and anatomical placement on that source. The unchecked state requests
natural, unmarked skin. The setting overrides conflicting draft/wishes and marks
prepared prompts for update when changed. The server appends its full English
tattoo instruction to enhanced and manually supplied generation prompts, replacing
its previous suffix without duplication and enforcing the final character limit.
The choice is stored as `keep_tattoos` in each job snapshot; new series reset it
to false. No schema migration is required.

Generate creates one independent ImageGeneration row per original, with its own
prompt, provider call, status and download. A series request is idempotent per
owner; its per-original request keys are stable. All rows and any credit
reservations commit together before workers can see the series. A failed setup
rolls back the whole series and removes partial temporary input copies. Once
queued, provider failures are independent per image. Mode, series ID, original
name/order and replacement roles are stored in each job's snapshot.

The existing storage switch and TTL also apply. Every temporary job gets its own
copies of shared references before the series commits, so cleanup after one
generation cannot remove another queued job's inputs. Download completed
temporary results before their individual deadlines. Original/shared draft
uploads still expire after IMAGE_REFERENCE_TTL_SECONDS; allow enough time for
prompt preparation when using large series. No additional migration is required
beyond the existing Image studio and retention migrations.

Endpoints: `POST /api/images/replacements/prompt` prepares one original's prompt;
`POST /api/images/replacements/generate` accepts an array of originals with their
individual prompts and returns their separate generation jobs. History remains
available through `GET /api/images`, scoped to the authenticated owner.

`POST /api/images/{id}/regenerate` creates an explicit new attempt using the stored
prompt, ratio and resolution, plus current provider/storage settings. Send a new
`requestKey`; reuse it after an uncertain HTTP response to retrieve the same job.
An optional ordered `references` list can replace deleted temporary uploads with
fresh owned image assets; its length/order must match the original source map.
Role/extra metadata is rebound to the new IDs. The original result is untouched
and `regeneratedFrom` identifies it. Only Ready, Error and Expired jobs can be
regenerated. Active and Review jobs cannot be resubmitted through this endpoint.

The browser retains selected File handles in memory while the app is open. For
temporary jobs it reuploads those source files before explicit regeneration,
without extending retention of old server inputs or outputs. After a reload,
deleted temporary sources must be attached again in a new draft. Persistent
references that remain available can be reused after reload. Regeneration uses
the same credit reservation path as a normal generation (currently disabled by
the platform-wide setting), and it never runs other originals in the series.
