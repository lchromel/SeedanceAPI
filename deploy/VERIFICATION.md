# Local verification — 2026-09-19

## Editing modes, identity controls and individual regeneration — 2026-11-03

- Django: 135 tests on SQLite (133 passed, two PostgreSQL locking tests skipped).
  Eight new tests cover makeup/hair identity scope, wardrobe/body-sheet views,
  caption-to-image mapping, role/owner/combined input limits, identity footer
  replacement, individual/idempotent regeneration, rejection of active/review
  jobs, and regeneration using fresh uploads after old temporary inputs were
  deleted. The previous output remains available until its own retention deadline.
- Frontend: seven tests passed, including three queue tests for real overlap,
  concurrency limits, stopping queued work while preserving active work and
  retrying failed items without discarding successful rows. TypeScript/Vite
  build, Python lint, migration drift and whitespace checks passed.
- Browser: checked all four mode entries, focused-mode inputs, shared iris color
  across modes and its persistence after reload, the six-color list with Blue as
  default, two extra uploads with independent captions and their exact labels.
  Regenerated one existing mock result and observed a new Ready job/download with
  the old Ready result still present. Desktop 1440 px and mobile 390 px checked;
  mobile document width remained 390 px, no visible broken images and no captured
  warnings/errors. Temporary viewport override was reset afterwards.
- Screenshots: `/private/tmp/seedance-editing-desktop.jpg` and
  `/private/tmp/seedance-editing-mobile.jpg`. File-picker automation was unusually
  slow; no production image-generation request was made during browser checks.
- Vision/DeepSeek responses in backend tests were mocked. Actual face/eye-color,
  makeup, hair and clothing fidelity, live model latency/rate limits and Railway
  deployment were not tested. No schema migration is required.

## Location staging and tattoo setting — 2026-11-03

- Django: 127 tests on SQLite (125 passed, two PostgreSQL locking tests skipped).
  Five new regression tests cover original-specific camera/environment context
  with a shared location, location focus during vision recovery, default removal
  and explicit preservation through enhancement/generation, repeated suffix
  replacement, final prompt limits and shared model/location reference roles.
- TypeScript/Vite production build, four frontend tests, Python lint, migration
  drift and whitespace checks passed.
- Browser: verified the Keep tattoos checkbox is initially unchecked, checking it
  shows the model/original preservation explanation, and unchecking restores the
  removal explanation. Screenshot: `/private/tmp/seedance-tattoos-default.jpg`.
- Model responses in these tests are mocked. No real visual output, paid model
  inference or Railway deployment was tested for this revision.

## Image vision response recovery — 2026-11-03

- Django: 122 tests on SQLite (120 passed, two PostgreSQL locking tests skipped).
  Nine new vision tests cover string/reordered labels, mapping/fenced formats,
  duplicate and zero-based IDs, malformed or partial aggregate responses, actual
  source-image isolation during recovery, preserved ordering into DeepSeek and
  failure handling that never launches image generation or changes credit state.
- Python lint and whitespace checks passed. No schema migration is needed.
- One diagnostic call with synthetic solid-color images and the local tokens.txt
  key returned HTTP 404 InvalidEndpointOrModel.NotFound for the default vision
  model. Live response-format verification was therefore unavailable for that
  credential/model combination; this does not establish the deployed service's
  model configuration. No user photographs were sent by this diagnostic.

## Photo series verification — 2026-11-03

- Django: 113 tests on SQLite (111 passed; two PostgreSQL locking tests skipped).
  Eight replacement tests cover per-original prompts/jobs/reference ordering,
  original proportions, individual vision inputs and wishes, absent-role defaults,
  reused reference roles, owner isolation, series idempotency, all-or-nothing
  credit reservation and temporary shared-reference cleanup/rollback.
- TypeScript/Vite production build, four frontend tests, Python lint, migration
  drift and whitespace checks passed.
- Browser: uploaded two originals and a shared model reference, entered two
  different prompts, queued two independent mock generations and observed two
  Ready results with separate downloads. Output dimensions were 2368×1776 and
  1776×2368, preserving the two originals' proportions. Desktop 1440 px and mobile
  390 px layouts checked; mobile had no horizontal overflow or broken images.
  No browser warnings/errors were observed in the checked flow.
- Real model calls/visual replacement quality and Railway deployment were not
  exercised. Browser generation used the explicit local mock provider; DeepSeek
  UI was disabled without a key. Vision/DeepSeek request behavior was verified
  with test responses in the backend suite.

## Temporary Image studio storage — 2026-11-03

- Django: 105 tests on SQLite (103 passed, two PostgreSQL locking tests skipped).
  Ten retention tests cover private temporary uploads/previews/downloads, deletion
  after generation, TTL expiration without application cleanup, independent copies
  for concurrent jobs, inline provider inputs, storage-mode changes, safe handling
  of expired input and unknown submissions, rollback and no S3 fallback.
- The temporary store uses a clock-controlled Redis test double in these tests;
  an actual Railway Redis instance and its persistence/backups were not exercised.
  Existing S3 upload regression coverage also passed.
- TypeScript/Vite production build, Python lint, migration drift and whitespace
  checks passed. Browser verification of this revision remains unavailable because
  the connected browser session was unavailable. No paid model requests were made.

## Original verification

- Django: **33 tests passed against PostgreSQL 16**, including concurrent reservations from two projects and duplicate generation requests. No skips.
- Frontend: **4 tests passed**, covering 30-second viewing segments, shorter provider jobs, boundary crossings, errors and ambiguous submissions.
- TypeScript and Vite production build passed. Python lint and migration drift checks passed. Production Django security checks passed with synthetic configuration (no external connection).
- Browser: sign in, image upload and selection, persistence across reload, generation states, credit balance/history, motion-reference upload and duration, sidebar icons, full export, and narrow-screen navigation were exercised. At 390px viewport width the document had no horizontal overflow. No browser console errors observed in the checked flow.
- End-to-end **local simulation**: two 90-second parts, six 30-second clips, final MP4 verified with ffprobe: H.264 + AAC, 720×1280, 180.021333 seconds.
- No paid model requests, production secrets, real invoices, Railway deployment, or public publication were used.

## Not yet validated live

- Docker image build (Docker was unavailable locally).
- Actual Railway service wiring, Redis delivery, S3 credentials and bucket policy.
- BytePlus model entitlement, input-reference restrictions, generated content continuity, real latency/cost, and provider-specific privacy terms.

The local preview uses SQLite, a test account, explicitly labeled mock generation and a mock job runner. PostgreSQL was used separately for the backend verification suite. The mock provider creates neutral video and silent audio; it is not an AI generation result.

## Image studio verification — 2026-11-02

- Django: 82 tests on SQLite (80 passed, 2 existing PostgreSQL locking tests skipped).
  Fifteen image tests cover private uploads/history/media, reference validation,
  vision-to-DeepSeek context, supported output dimensions, credit reservation and
  settlement, idempotent requests, ambiguous submission/reconciliation and download recovery.
- Frontend: 4 existing tests passed; TypeScript/Vite production build passed.
  Python lint, migration drift and whitespace checks passed.
- Browser: uploaded two references in one chooser, entered a prompt, changed the
  aspect ratio, generated and downloaded a simulated JPEG, and switched between
  Images and Video with the image draft intact. Expanded and collapsed navigation
  checked. Desktop 1440 px and mobile 390 px checked; no horizontal overflow or
  broken images in the checked mobile view.
- Paid Seedream/DeepSeek calls, model entitlement, production S3/Redis delivery,
  production migration and deployment were not exercised. Local browser preview
  used a separate temporary SQLite database and the explicit mock provider.
