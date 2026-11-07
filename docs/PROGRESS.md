# Autonomous run progress

## Round 13: translation style and native script

- Current task: identify the active Sarvam translation model/settings and mixed-script source; verify official Mayura API parameters; implement per-project Natural/Pure style, script-aware translation, safe style switching, and tests.
- Initial model/mode findings: `SARVAM_TRANSLATION_MODEL` defaults to `mayura:v1`; `SARVAM_REWRITE_MODEL` defaults to `sarvam-105b-conversations`. Existing translation calls use Mayura `modern-colloquial` by default or `classic-colloquial` for a respectful tone, and use `formal` only when the configured translation model is `sarvam-translate:v1`. No `output_script` is sent. The mixed Latin words therefore come from Mayura's default script behavior and are stored as returned, with no target-script postprocessing.
- Official reference checked: Mayura supports the app's 10 targets (English plus nine Indian languages), a 1,000-character limit, modes `formal`, `modern-colloquial`, `classic-colloquial`, `code-mixed`, and output scripts `fully-native` and `spoken-form-in-native` (the latter for spoken style). `sarvam-translate:v1` is formal-only, supports 2,000 chars, and does not support `output_script`; Natural will use `modern-colloquial` + `spoken-form-in-native`, Pure will use `formal` + `fully-native`, and English keeps its native Latin script. Reference: https://docs.sarvam.ai/api-reference/text/translate-text.
- Implemented: Mayura now receives explicit style mode and native-script output values; inputs over 1,000 characters are split; Mayura failures fall back to formal-only `sarvam-translate:v1` with a warning log. Project style is stored, new dub and settings expose Natural/Pure choices, and style switching queues eligible clips with edit/lock/verification guards and asks for clip count, automatic warnings, and estimated credits first. Added a native-script advisory and Railway diagnostic script.
- Focused validation: `python -m compileall -q backend scripts/check_translation_styles.py tests/test_translation_styles.py` passed; `python -m pytest tests/test_translation_styles.py tests/test_llm_client.py -q` passed (37 tests). `git diff --check` passed.
- Final validation: `python -m pytest tests -q` passed (exit 0; only existing FastAPI lifecycle deprecation warnings). `cd frontend && npm run build` passed, including the API-prefix check, TypeScript, and Vite production build. The Railway diagnostic script was not run against live Sarvam because it makes billable network calls; no key or provider output was accessed.
- Completed and committed: `4a0e29e` (will be finalized after recording the final hash); the changes include provider settings, project style switching, script advisory, Railway diagnostic, and mocked tests. Push `main` after amending this progress note with the final commit hash.
- Decisions: keep translation requests clip-by-clip and below 1,000 characters; never retranslate edited, locked, or currently verified clips during a style switch. Never access `/data/voices` or the database file.

## Round 12: Mark verified

### Current status

- Current task: Round 12 complete; validate, commit, and push the tested verification fix.
- Required validation before the single push: `python -m pytest tests -q` and `cd frontend && npm run build`.
- Reproduction finding: local Playwright showed a browser exception, `p.verify is not a function`, because `ReviewPage` did not pass `verify` and `unverify` callbacks into `ClipRow`; clicking the button sent no request and displayed no user-facing error. Therefore the direct cause was a missing UI callback (with a silent browser-side failure), not automatic rejection or a provider/audio check. Separately, the endpoint response did not include the updated clip/counts, and Balance timing cleared approval for any nonzero move. A backend test exposed the response contract gap; the corrected small-move fixture passes.
- Completed implementation: persist `auto_status` separately from user approval; only a matching text/take/rounded-placement fingerprint counts as Verified; verification returns the updated clip and counts without provider/audio checks; edits clear approval, small balance moves retain it and large moves report cleared clips. Added optimistic UI updates, explicit Undo, Verify all confirmation, advisory check display, and Hindi/Kannada number/name transliteration coverage.
- Reproduction: the browser raised `p.verify is not a function`; the callback was missing from the `ClipRow` props, so clicking Mark verified sent no request. The automatic-flag warning also remained visible after approval because the API did not return the effective user-approved clip state, while Balance timing invalidated approval on any move.
- Final local Playwright: with mock providers and a temporary data directory, verified Hindi clips 2, 1, and 3 sequentially. Each request was POST with `{confirm_override:false,note:null}` and returned HTTP 200 with the updated clip and counts. UI reached 3 verified, persisted after reload and Balance timing, and opened export with no warning (export endpoints were mocked; a real artifact was not generated in this UI check).
- Final backend validation: `python -m pytest tests -q` passed (all tests; four existing FastAPI lifecycle deprecation warnings).
- Final frontend validation: `cd frontend && npm run build` passed (API-prefix check, TypeScript, Vite production build). The build updates the tracked frontend bundle under `backend/static`.
- Next: commit this completed Round 12 change set and push `main` once.
- Decisions: keep automatic checks advisory after a human approval; do not alter translation, timing scheduling, or TTS generation except to preserve approval across sub-0.5-second balance moves. Never touch `/data/voices` or the database file.

## Completed work before Round 5

- Provider swap: `20eb6a5`.
- Timing and sync: `aeddca0`.
- Dubbed playback: `3c3b4c4`.
- Terms and edit persistence: `d063b83`.
- Attempt history: `585d67f`.
- Editor UX: `96d5902`.
- Resilience, checkpoints, media recovery and bilingual smoke coverage: `571b987`, `5b7576a`, `1b309d2`, `50d7e90`, `c5acef8`, `77cfb36`.
- Round 4 storage handling: `b0e6efc` and `0a72d99`.

## Round 5: finish storage, small volume, README

### Part A

- Complete and pushed as `0a72d99`. Finished upload preflight, storage visibility, deletion and cleanup, and missing-file state. Verified `.gitignore` patterns and confirmed the test key-looking strings are fake fixtures without printing values.
- Backend tests and frontend build passed before push.

### Part B

- Complete and pushed as `58d05ed`. Added an 80% volume budget, a two-finished-project limit, oldest-finished eviction, a 40 MB cap, verified 720p H.264/AAC working copies, export cleanup and expiry (default 24 hours). Active and interrupted projects are not eligible for eviction. The compressed copy is used for preview, processing and export; the original is deleted only after verification.
- Tests cover eviction ordering, verified-copy failure, export expiry, storage status and ENOSPC handling. Backend tests and frontend build passed before push.
- Disk measurement used an FFmpeg-generated 20-second 720p test video with a sine tone. Original: 3,300,448 bytes. Compressed copy: 2,326,021 bytes. The mock project with two retained language outputs used 7,952,510 bytes before replacing the original and 6,978,103 bytes after, a 12.3% reduction.

### Part C

- Current task: simplify the root README and remove generated documentation imagery and capture tooling. Do not change app code. Run backend tests and frontend build before pushing.

### Decisions

- Push after each of Parts A, B and C, not after individual subtasks.
- Never delete voice files under `/data/voices` or the database file.
- Automatic age-based project cleanup remains disabled. The only new setting is export retention, default 24 hours.
- Earlier source-retention guidance is superseded: the verified compressed working copy is used for processing and export.

## Round 6: source video and storage budget

### Current task

- Replaced upload-time 720p compression with source-preserving video handling. Compatible H.264 8-bit MP4 files remain unchanged at their original resolution. Only incompatible browser video is converted in one no-scale, single-threaded H.264 pass. A failed conversion retains the original, marks the preview limitation in the upload response, and does not fail upload. Export and rebuilt previews copy the video stream and replace audio; the export path uses faststart.
- Raised the upload limit to 100 MB and set the storage budget to 350 MB, retaining the two-finished-project limit. Removed the 720p upload note and updated the README storage/video statements.
- Added regression coverage for keeping compatible H.264 uploads unchanged, preserving originals after failed conversions, returning successful uploads despite conversion failure, and video-copy audio-replacement export muxing. Full backend suite (`python -m pytest tests -q`) and frontend build (`npm run build`) passed.

### Decisions

- Treat any nonzero rotation metadata as requiring compatibility conversion. Keep the uploaded audio stream when possible; the dubbed export replaces it with AAC speech audio.
- Apply the 350 MB budget as a hard ceiling, capped by the actual filesystem size if that is smaller.

## Round 8: HEVC and playback

### Done

- Read the Round 8 request and the current progress history. Confirmed the working branch is `main` and there were no uncommitted changes at the start of this round.
- Step 1 reproduced. Added `tests/fixtures/generate_hevc_rotated.py`; it generates into a caller-provided temp location, not the repository. `libx265` is available. Generated a 5-second, 1920x1080 HEVC Main, full-range `yuvj420p`, 90-degree display-rotation MP4 with AAC audio (2,812,095 bytes).
- Running `app.create_browser_compatible_copy(input, output)` raised `RuntimeError: Transcoded video did not pass browser compatibility checks: pixel format yuvj420p is not 8-bit 4:2:0`. The generated conversion was captured before the function removed it. It was H.264 High, 1080x1920 (rotation applied), `yuvj420p`, full range, AAC, duration 5.013 seconds. The guard rejected a valid 8-bit 4:2:0 output because it only accepted the literal pixel format `yuv420p`. The input also exposed a 13 ms duration expansion from frame-rate conversion, which Step 2 will avoid unless VFR requires it.

### Completed

- Step 1: reproduced the reported full-range rotated HEVC conversion failure on a real generated MP4.
- Step 2: normalized full-range input to 8-bit limited-range `yuv420p`; the output now passes ffprobe and the app guard.
- Step 3: added persisted preview state, single in-process background conversion, restart scheduling, status/retry endpoints, and job waiting. Media GETs do not launch conversion.
- Step 4: added pending/failure/retry UI and one automatic retry per video load; media route recovery catches ordinary exceptions and logs path, size, and ffprobe stderr. Track URLs are resolved through the configured API origin.
- Step 5: fixed mock playback so `GNANI_MOCK` yields valid non-empty WAV data and `LLM_MOCK` is used by pipeline LLM calls. A real API smoke confirmed nonzero take and track durations for both languages.
- Step 6: added preview-state/retry, invalid prepared-copy fallback, and audio-route tests. Full backend suite and frontend production build pass.
- Step 7: completed a real local HTTP upload/job/playback smoke with both mocks on. The job completed with clips for Hindi and Kannada, media and Range responses passed, and ffprobe validated outputs. Measured the requested 20-second 1080p HEVC conversion.

### Decisions

- The configured 350 MB project budget now measures files managed under `ANEK_DATA_DIR`, while upload preflight continues to check actual free filesystem space. The previous global-volume-used comparison blocked every local smoke upload on the host volume.
- Deterministic LLM mock translations use short Hindi/Kannada sample text. They make the documented mock workflow exercise actual pipeline routes without contacting paid providers.

### Exact command results

- Conversion fix: `python -` called `create_browser_compatible_copy` on `%TEMP%\anek-round8-hevc-rotated.mp4`; result `conversion=ok`, `_browser_transcode_reason(output)=None`. `ffprobe` output: H.264 High, `1080x1920`, `yuv420p`; AAC LC; MP4; `5.013000` seconds; no rotation metadata.
- Storage debugging: first live upload returned HTTP 507 because `ensure_storage_room` compared volume-wide `disk.used` against the app's 350 MB budget. After switching the budget calculation to `managed_data_bytes()`, upload returned HTTP 200. The actual filesystem free-space check remains active.
- Initial mock job attempt: upload and video Range route succeeded, then the job failed with `ValueError: Translation provider returned no usable text for human review`. Server traceback pointed to `run_quality_pass` -> `paragraph_translation_candidates`; `LLM_MOCK` had not been wired into this path and the Sarvam provider was attempted. The pipeline now uses `MockLLMClient` when `LLM_MOCK=1`, and mock Gnani synthesis now writes valid WAV.
- Real HTTP smoke command: a Python `requests` script started `python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765` with `GNANI_MOCK=1`, `LLM_MOCK=1`, and `ANEK_DATA_DIR` set to a new `%TEMP%` directory; it POSTed `/api/projects`, PUT `/api/projects/{id}/video` with the 5-second rotated HEVC fixture, polled `/preview-status`, GET `/video` with `Range: bytes=0-1023`, POSTed `/start` with stock voice, and polled `/summary`.
- Smoke result: `upload_status=200`, `preview_status=ready`, source video Range `206` with `Content-Range`, job `complete`, one clip and dub per target, and project `complete`. HTTP media reads returned `200 video/mp4`; media Range reads returned `206 video/mp4 bytes` with `Content-Range` for both Hindi and Kannada. Each output ffprobed as H.264/yuv420p + AAC, `5.013000` seconds, and passed faststart validation. Both languages had one non-overlapping clip. Each clip take and dubbed/source/background track returned non-empty `audio/wav`; clip take durations were `3.021438` and `3.374313` seconds, and full dub track durations were `5.013000` seconds. `verify.log` search for `Traceback|ERROR|exception|Internal Server Error` returned no matches.
- 20-second 1080p HEVC measurement: generated a 1920x1080, 30 fps, full-range HEVC + AAC fixture, remuxed 90-degree rotation metadata, then timed `create_browser_compatible_copy`. Source `11,182,742` bytes; conversion `13.949` seconds; output `10,984,170` bytes. ffprobe: H.264, `1080x1920`, `yuv420p`, AAC, `20.010000` seconds, no rotation metadata.
- Final `python -m pytest tests -q`: passed, 113 tests (100%); only FastAPI `on_event` deprecation warnings.
- Final `cd frontend; npm run build`: passed; API prefix check scanned two source files, TypeScript and Vite build passed (460 ms).

### Exact command results

- `git status --short`: empty.
- `git branch --show-current`: `main`.
- Encoder inventory: `ffmpeg -hide_banner -encoders 2>&1 | Select-String -Pattern 'libx265|hevc_nvenc|hevc_qsv|hevc_amf|libx264'` listed `libx265`, `hevc_amf`, `hevc_nvenc`, `hevc_qsv`, and `libx264`.
- Fixture commands (exact): `ffmpeg -y -hide_banner -v warning -f lavfi -i testsrc2=size=1920x1080:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 -t 5 -vf format=yuvj420p -c:v libx265 -preset ultrafast -x265-params log-level=error -color_range pc -tag:v hvc1 -c:a aac -b:a 128k -movflags +faststart <temp>.base.mp4`; then `ffmpeg -y -hide_banner -v error -display_rotation:v:0 90 -i <temp>.base.mp4 -map 0 -c copy -movflags +faststart <temp>.mp4`.
- Input ffprobe command: `ffprobe -v error -show_entries stream=codec_type,codec_name,profile,pix_fmt,width,height,color_range:stream_tags=rotate:stream_side_data=rotation:format=format_name,duration -of json <temp>.mp4`. Exact salient output: video `hevc`, profile `Main`, `1920x1080`, `yuvj420p`, `color_range=pc`, `side_data_list.rotation=90`; audio `aac`, profile `LC`; container `mov,mp4,m4a,3gp,3g2,mj2`; duration `5.000000`.
- Current conversion output, same ffprobe command: video `h264`, profile `High`, `1080x1920`, `yuvj420p`, `color_range=pc`; audio `aac`, profile `LC`; MP4-family container; duration `5.013000`. Exact failing check: `pixel format yuvj420p is not 8-bit 4:2:0`.
- Real conversion invocation: Python imported `backend.app`, wrapped `_browser_transcode_reason` only to copy the produced file before validation cleanup, then called `app.create_browser_compatible_copy(source, output)`. It returned the above RuntimeError. This reproduces the report against the actual function.
- Step 2 done. Updated conversion to explicitly normalize full-range video to limited-range `yuv420p` and retain the single-threaded, no-scale, faststart conversion. Re-ran `create_browser_compatible_copy` on the same fixture: `conversion=ok`, `_browser_transcode_reason(output)=None`; ffprobe reports H.264 High, `1080x1920`, `yuv420p`, AAC LC, MP4, 5.013 seconds, and no rotation metadata. Command: `python -` importing `create_browser_compatible_copy` and `_browser_transcode_reason` with `%TEMP%\anek-round8-hevc-rotated.mp4` and `%TEMP%\anek-round8-hevc-fixed.mp4`; then `ffprobe -v error -show_entries stream=codec_type,codec_name,profile,pix_fmt,width,height,color_range:stream_side_data=rotation:format=format_name,duration -of json <output>`.
- Step 3 implemented. Added project preview status/path/error columns with migration. Upload preserves the original and schedules a single in-process conversion task; startup queues persisted pending previews. Ready projects serve the converted path, failed projects retain the original. Added preview status and explicit retry endpoints; media GET no longer invokes conversion and returns 202 while the project preview is pending. Jobs wait for the conversion task before processing.
- Step 4 started. Frontend polls preview status, displays pending/failure reason and a Retry preview button. Audio track URLs now pass through `apiUrl()` so remote API origins work for `<audio>` and download links. Media routes catch all ordinary exceptions and retain the documented fallback chain.
- Targeted regression tests: `python -m pytest tests/test_api_destinations.py -q` passed 27 tests before adding the two new status/audio tests. Frontend build passed: `cd frontend; npm run build` (API prefix check passed; Vite build completed in 934ms). Full suite was then run and surfaced one stale smoke fixture that lacked the new ready preview fields; updated that fixture. Full suite rerun is pending.

Exact ffprobe outputs:

```json
{
  "programs": [],
  "stream_groups": [],
  "streams": [
    {"codec_name": "hevc", "profile": "Main", "codec_type": "video", "width": 1920, "height": 1080, "pix_fmt": "yuvj420p", "color_range": "pc", "tags": {}, "side_data_list": [{"rotation": 90}]},
    {"codec_name": "aac", "profile": "LC", "codec_type": "audio", "tags": {}}
  ],
  "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.000000"}
}
```

```json
{
  "programs": [],
  "stream_groups": [],
  "streams": [
    {"codec_name": "h264", "profile": "High", "codec_type": "video", "width": 1080, "height": 1920, "pix_fmt": "yuvj420p", "color_range": "pc", "tags": {}},
    {"codec_name": "aac", "profile": "LC", "codec_type": "audio", "tags": {}}
  ],
  "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.013000"}
}
```

## Round 9: red clips and clean review studio

- Tasks done: Part A complete; Part B implementation and visual checks complete, awaiting its dedicated checkpoint push.
- Part A checkpoint: committed and pushed to `main` as `bf3d7ff2a5c0d62803f9f18d1ffa9506326728b1` (`Fix red clip recovery and review reasons`).
- Part B validation: after the mobile CSS fix, `python -m pytest tests -q` passed and `cd frontend && npm run build` passed (API prefix check, TypeScript, Vite build). Playwright mock screenshots passed for verified, review, red, side-by-side, and 375px mobile with no horizontal overflow.
- Verification limit: the original production project media and database were not accessed, so the reported real clip with a 3.9-second slot could not be replayed. Timing behavior is covered by the all-language parametrized tests; visual red-state behavior used a local mocked clip.
- Current task: commit and push Part B.
- Next task: final report with both checkpoint hashes, timing test/build results, and the production-sample verification limit.
- Decisions: follow the attached Base44 screenshots for layout only; retain Anek branding. Do not touch /data/voices or the database file. Validate and push once after each part.
- Step 1 done: timing rewrites now submit only the active source segment, label neighboring source and translation as read-only context, enforce the actual character limit on returned text, and reject copied four-word phrases from neighboring source/translation. Added parametrized regression coverage for all 10 target languages. `python -m pytest tests/test_phase3b.py -q` passed (32 tests).
- Current task: finish Part A remaining clip segmentation, term rule actions, missing-audio UX, and plain-language red reasons.
- Step 2 done: clips under five seconds now merge with the next adjacent span when the combined range stays within the clip cap; incomplete sentence cuts still merge and timing bounds remain unchanged. Added a timing regression test. `python -m pytest tests/test_phase3b.py -q` passed (33 tests).
- Step 3 done: source correction now saves into the clip and language reports, marks the dub for re-speech, and can create a case-insensitive mishearing phrase rule for the project or global memory. API responses distinguish missing take files and include a plain-language reason and suggested action. Added source/API regression coverage.
- Step 4 done: review cards now collapse to one-line rows with only the active clip expanded; source and translation editing, single “Save & re-speak”, plain-language reason/action, number/name chips, collapsed takes/pronunciation/details, and audio-removed recovery are in place. Header utilities moved into More; audio layers are collapsed and only show when tracks exist. Local Playwright mock screenshots were captured and viewed for verified, review, red, and side-by-side states. The valid WAV fixture displayed a 2-second duration, the player showed video content instead of black, and no `∅` or `over_slot` text appeared.
- Part A validation: `python -m pytest tests -q` passed; `cd frontend && npm run build` passed (API prefix check, TypeScript, Vite build).
- Part B visual validation: Playwright mock captures for verified, review, red, side-by-side, and 375px mobile layouts. The mobile pass found an intrinsic grid overflow (527px content at a 365px viewport); CSS now constrains the review grid and its children. Re-run after the final build.
- Current task: create the Part A checkpoint commit and push; then separately commit/push the Part B UI and mobile fix.

## Round 10: final stabilization

- Done: progress log read; Round 9 is completed and pushed as `6a074a7af5dcbcb270a97d0f01ea8500f7370c81`.
- Done: Part A cause isolated and fix/test changes underway; Railway required key presence confirmed; obsolete provider variable names inventoried.
- Current: finish Part A hotfix tests and checkpoint/push. Root cause: paragraph candidates are candidate-major (up to three full arrays), but retry context indexed this outer array by clip index as though it held one translation per clip. At clip index 3 this raised IndexError, while smaller lists could select the wrong neighbor context.
- Next: Part B single Sarvam provider chain; Part C honest project completion/failure states and validation. Push only after Part A and Part C, after full backend tests and frontend build pass.
- Decisions: follow the attached Round 10 specification as the latest provider instruction. Never print secret values. Do not inspect or modify `/data/voices` or the database file. Railway UI/API may be used to remove obsolete legacy LLM variables and confirm required variable names exist; variable values must remain hidden.
- Exact results (step 1): `git status --short` showed only this progress-file edit; `git branch --show-current` returned `main`; `railway --version` returned `4.42.1`; `railway whoami` confirmed an authenticated session (identity omitted); `railway status` selected Project `lucky-freedom`, Environment `production`, Service `anek`. `railway variable delete --help` and `railway variable list --help` confirmed explicit service/environment support. No values were read to terminal output.
- Exact results (step 2): sanitized `railway variable list --service anek --environment production --json` showed `SARVAM_API_KEY` and `GNANI_API_KEY` both present and non-empty. Six obsolete LLM variable names were present. No additional provider-specific variables were configured. Deletion is queued until the code no longer depends on the fallback chain, to avoid restarting the current deployment with missing configuration.
- Exact results (step 3): inspected `paragraph_translation_candidates` and retry block. Introduced `build_neighbor_context(rows, translated_by_index, index)` using the selected candidate and original clip indices. Resume persists selected translations at stage `translation`; retries pass only missing clip rows; selected-clip regeneration no longer errors when its translation is absent and only iterates the target source index.
- Exact results (step 4): `python -m pytest tests/test_phase3b.py tests/test_resilience_smoke.py -q` passed (43 tests). Full `python -m pytest tests -q` passed (126 tests; FastAPI lifespan deprecation warnings only). `cd frontend && npm run build` passed (API prefix scan, TypeScript, Vite build).
- Current: complete provider cleanup and stabilization changes. Next: validate, push, remove unused Railway variables, then run the production provider check.
- Exact results: provider chain cleanup and Railway provider check pending.

### Round 10 completion notes

- Part B: Sarvam is the only translation and QA provider; Saaras v3 handles transcription. The legacy LLM fallback was removed. A shared five-minute circuit breaker and capped request concurrency/rate gate protect provider calls.
- Part C: completed-language checkpoints are retained independently; a failed language can be retried from its saved transcript and completed clips. Project summaries expose per-language completion counts and errors, and the review page shows a language-specific Processing, Interrupted, or Failed state instead of presenting partial clips as complete.
- Diagnostics: `scripts/check_providers.py` checks translation for all configured target languages and Saaras transcription. `scripts/diagnose_project.py PROJECT_ID` prints read-only checkpoint and clip counts without displaying environment values.
- Current: run tests/build, push the final Round 10 commit, remove obsolete Railway variable names, wait for deployment, then run the live provider check.
- Next: report final hashes, test results, provider output, and any failed languages.
- Decisions: the provider script performs one short translation request for each target language and one short Saaras request. The Gnani line reports configuration presence only to avoid making a paid voice request.


- Validation before Part C push: python -m pytest tests -q -o addopts='' passed (121 tests, 4 existing FastAPI lifecycle deprecation warnings); cd frontend && npm run build passed (API-prefix scan, TypeScript, Vite). git diff --check passed. The repository search for retired provider names in backend, frontend source, config, README, docs, scripts, and tests returned no matches.
- Push checkpoint: pending.

- Deployment check follow-up: the active Railway container lacked scripts/check_providers.py because the Dockerfile copied only backend files. Added an explicit scripts copy in the final image. The first remote invocation exited 2 with a missing-file error; the live provider check is deferred to the updated deployment.

- Docker follow-up validation: python -m pytest tests -q -o addopts='' passed (121 tests, 4 lifecycle deprecation warnings); cd frontend && npm run build passed.
- Current: commit and push the image packaging fix. Next: wait for Railway deployment, run python scripts/check_providers.py remotely, inspect only the sanitized status output, and confirm the obsolete variable names remain absent.

- Live check on deployment 6eefea6-db49-4921-b2c0-fd29ad63694a: Bengali, Gujarati, Hindi, Kannada, Malayalam, Marathi, Punjabi, Tamil, Telugu, Saaras STT, and Gnani key presence were OK; English failed. The check used English as both source and target, which tested a no-op rather than English translation. Fixed the probe to translate Hindi into English and made same-language project targets reuse source text. Re-run against a fresh deployment is required.

- English-target fix validation: python -m pytest tests -q -o addopts='' passed (122 tests, 4 existing lifecycle deprecation warnings); cd frontend && npm run build passed. The corrected provider check awaits deployment.
- Final live result: Railway deployment a0dd3cdb-fbcb-47e7-a0bf-b9b0398c60b8 ran `python scripts/check_providers.py`: Bengali OK, English OK, Gujarati OK, Hindi OK, Kannada OK, Malayalam OK, Marathi OK, Punjabi OK, Tamil OK, Telugu OK, Saaras STT OK, GNANI_API_KEY CONFIGURED. SARVAM_API_KEY and GNANI_API_KEY were present and non-empty; all six obsolete variable names were absent after removal.
- Round 10 commits: Part A hotfix `7da1f877a05bdffacec9135316ebc8ac46da90f9`; provider and per-language status `1fb8a79`; image packaging fix `0d6b622`; same-language target fix `43b9544`. Final validation: 122 backend tests passed (4 existing lifecycle warnings) and frontend production build passed. Final live status all target-language checks passed.

## Round 11: verify, elastic timing, timeline

- Started from clean `main` at the end of Round 10. Scope follows the pasted request; never access `/data/voices` or the database file directly.
- Part A complete: persisted user verification independently from automatic status, fingerprint-bound verification, confirmation override, Undo verified, live row/header counts and Next problem skipping verified clips. Added clearer saved-text/audio-out-of-date wording and Natural spoken, Keep English technical terms, and Formal project style instructions to the Sarvam translation prompts. Added regression tests for verification counts, override confirmation, undo, and Hindi/Kannada natural-style requirements. Full backend tests and frontend production build passed; see Part A commit below.
- Part B current: implement and test the deterministic elastic scheduler, placement API, timing health, and export/preview placement integration.
- Part C next: timeline and transcript editor, continuous synchronized playback, drag timing, balance, undo/redo, mobile and visual review.
- Validation and pushes: after each Part, run `python -m pytest tests -q` and `cd frontend && npm run build`; push only after both pass.
- Decisions: use the generated mixed server-side dub preview for continuous synchronized playback because it keeps one timing authority across browsers. Keep screenshots and Playwright tooling out of Git. No tests/results are claimed until run.
- Part A reproduction: `python -m pytest tests/test_api_destinations.py::test_user_can_verify_a_saved_audio_take -q -o addopts=''` passed (1 test), confirming only that the old endpoint sets `status='green'`; it did not cover a separate durable user decision, fingerprint invalidation, or row/header consistency. Added migration fields for those missing semantics. No live DB was read or changed.
- Part A validation: `python -m pytest tests -q` passed; `cd frontend && npm run build` passed (API-prefix scan, TypeScript, Vite). Existing FastAPI lifecycle deprecation warnings remain.
- Part A commit: `9774cef` (awaiting push after documentation checkpoint validation).

### Round 11 checkpoint update

- Part A was pushed as `8ce3eb3` (correcting the earlier short hash recorded above).
- Part B complete: added a deterministic language-independent scheduler with 1.0s per-clip and cumulative drift bounds, 1.15x speed-up ceiling, 100ms tolerance, pause reset behavior, and final video-end / no-overlap validation. Added a language balance endpoint that preserves manual placements, clears verification if timing changes, rebuilds the mixed dubbed track and preview, and returns timing health. Invalid schedules are returned for review and are not persisted. The review timeline now shows placed speech, source slots, overlap markers, and a Concise-style suggestion when total speech exceeds available time.
- Part B tests: `python -m pytest tests -q` passed (158 tests; 4 existing FastAPI lifecycle deprecation warnings). `cd frontend && npm run build` passed (API-prefix check, TypeScript, Vite).
- Part B commit: pending.
- Part C next: rolling synchronized playback, playhead and list-follow behavior, drag timing with bounds and snapping, undo/redo, transcript layout and local visual QA.
- Part B limitation: the endpoint reports `shorten_required` and advises the Concise style but does not automatically call Sarvam to shorten and re-synthesize a clip. Automatic paid generation is deferred; the schedule never writes an overlapping placement.

### Part B push

- Part B commit and push: `80c4d14`.

### Part C complete

- The main dubbed player continues on the server-rendered mixed track. Playback time moves a timeline playhead, selects the active clip, and scrolls the transcript list to it. Selecting a clip seeks to its placed start and plays only its speech range.
- The timeline shows status-colored source slots, the placed speech fill, and an overlap marker. Dragging the speech bar adjusts its start within +/-1.0 seconds; an overlap is surfaced immediately and the server rejects invalid placements. Added per-timeline Undo and Redo, plus Balance timing.
- The selected transcript editor presents source and translated text in two columns on desktop and stacks them on mobile. Audio elements load metadata so existing take durations appear without first pressing Play. Verified clips no longer show contradictory automatic review warnings. Removed the accidental numeric `0` rendered next to Undo edit.
- Playback implementation choice: use the single mixed preview track rendered server-side, so preview and export share one placement and mix.
- Playwright QA ran against a local API server with `GNANI_MOCK` and `LLM_MOCK`, an isolated temporary data directory, and a 17.8-second generated source. The job completed with three clips in Hindi and Kannada. Actual clip statuses were review-needed; none was red because of slot length. All three takes were nonempty WAVs and reported durations through the browser. A routed visual fixture showed one verified, one review, and one needs-you state. Captured and viewed `timeline-statuses.png`, `timeline-drag.png`, and `timeline-balanced.png` under the system temp directory; screenshots and Playwright code are not committed.
- Part C validation: `python -m pytest tests -q` passed (158 tests; 4 existing FastAPI lifecycle deprecation warnings). `cd frontend && npm run build` passed (API-prefix check, TypeScript, Vite). The screenshots were captured and inspected; the first loaded take reports 2.856 seconds in Playwright.
- Part C commit: `8883663` (pushed to `main`).
- Limits: drag editing currently adjusts clip start only, not the end handle, and does not snap automatically to neighboring clips. The mock visual fixture used generated test-pattern video and synthetic transcript text, not user footage or paid providers. Part B automatic Sarvam shortening and re-synthesis remains deferred as recorded above.
