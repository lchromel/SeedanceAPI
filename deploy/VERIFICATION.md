# Local verification — 2026-09-19

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
