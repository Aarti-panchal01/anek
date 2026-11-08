# Go live checklist

## Before inviting users

### Vercel

- Root Directory: `frontend`
- Framework Preset: `Vite`
- Build Command: `npm run build`
- Output Directory: `dist`
- Set `VITE_API_BASE_URL` to the Railway service origin with `/api` at the end.
- Confirm the Production deployment is healthy and the latest `main` commit is deployed.

### Railway

- Deploy from the repository root using the included `Dockerfile`.
- Attach a persistent volume at `/data`.
- Keep the Railway service port setting aligned with the platform `PORT`; the app defaults to 8080.
- Add `SARVAM_API_KEY_1` through `SARVAM_API_KEY_5` and `GNANI_API_KEY` in the Railway production environment. The numbered Sarvam keys rotate when a key is rejected or quota-limited; legacy `SARVAM_API_KEY` is used only when no numbered keys are configured. Do not store credentials in Vercel or the repository.
- Gnani handles STT and TTS. Sarvam handles translation and the associated text workflow.
- Set `SARVAM_TRANSLATION_MODEL` and `SARVAM_REWRITE_MODEL` only when overriding their code defaults.
- Set `REQUIRE_ACCESS_AUTH=true`, `ANEK_ACCESS_PASSWORD`, and `ANEK_AUTH_SECRET` before exposing a private workspace.
- Set `CORS_ORIGINS` to the exact production Vercel origin, including scheme and no trailing slash. Keep it aligned with the domain assigned to the production deployment. Use `CORS_ORIGIN_REGEX` only when preview subdomains must also be allowed.
- Remove unused credentials and model settings for retired translation providers from the Railway environment.

## Verify the release

- Open the Vercel production URL and confirm `/health` reports service health.
- Sign in and confirm project listing and media playback work through `/api`.
- Upload a small video, inspect the local preview, run one mocked job in a non-production environment, and check provider status before paid production work.
- Review the Railway logs for provider names and timing values only. Never print environment variable values.
- Confirm disk usage and the persistent volume before running long jobs. Existing projects and exports are retained until manually deleted. The managed storage budget limits new uploads but never evicts existing projects.
- Confirm an export can be downloaded and remains available until its project is manually deleted.

## Auth and request limits

The workspace gate uses a signed bearer token after password login. The token expires after seven days. Sign-in is limited to eight attempts per source IP per hour. Request categories have hourly limits configured by `PER_IP_LIMITS`; the defaults are three voice clones, thirty previews, and five jobs per IP. A limited response includes `Retry-After` and a plain-language message.

Local unit coverage checks that unauthenticated requests are denied, valid signed tokens are accepted, and request limits return HTTP 429 with a retry interval. Do not test these controls against the production database.

## Names to configure

- Vercel: `VITE_API_BASE_URL`
- Railway: `SARVAM_API_KEY_1`, `SARVAM_API_KEY_2`, `SARVAM_API_KEY_3`, `SARVAM_API_KEY_4`, `SARVAM_API_KEY_5`, `SARVAM_API_KEY` (legacy fallback), `GNANI_API_KEY`, `SARVAM_TRANSLATION_MODEL`, `SARVAM_REWRITE_MODEL`, `ANEK_DATA_DIR`, `REQUIRE_ACCESS_AUTH`, `ANEK_ACCESS_PASSWORD`, `ANEK_AUTH_SECRET`, `CORS_ORIGINS`, `CORS_ORIGIN_REGEX`, `MAX_VIDEO_SECONDS`, `EXPORT_RETENTION_HOURS`, `GNANI_DAILY_LIMIT`, `MAX_CREDITS_PER_JOB`, `APP_NAME`, `NUMERAL_STYLE`, `ENABLED_LANGS`, `LOW_TRUST_VERIFY_LANGS`, `PER_IP_LIMITS`, `FIT_TOLERANCE`, `MAX_STRETCH`, `SIMILARITY_THRESHOLD`, `CONSENT_MATCH_THRESHOLD`
- Development/test only: `GNANI_MOCK`, `LLM_MOCK`
