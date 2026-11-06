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
