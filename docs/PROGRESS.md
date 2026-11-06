# Autonomous run progress

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
