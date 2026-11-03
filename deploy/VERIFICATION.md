# Local verification — 2026-09-19

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
