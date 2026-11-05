# Autonomous run progress

## Tasks done

- Already complete before this run: API URL prefix check (`bc0c3b9`); preview ffmpeg cap and fallback (`90bbb1d`); Range support (`ea6a5a7`); shared source transcription; timing recovery across ten languages (`7ad4dbd`).
- Task 1, LLM provider swap: `20eb6a5` (pushed). Backend suite passed; frontend build passed.

## Current task

- Task 2: Timing and sync, all ten languages. Not started.

## Next task

- Task 3: Dubbed playback.

## Decisions

- Preserve all user-facing model-name removal.
- Do not print environment variable values.
- The installed Sarvam SDK does not expose the requested 30B chat model; use the supported 105B conversations endpoint for rewrite/QA.
- Keep user-facing provider/model names hidden. Keep the existing fallback chain with Gemini last.
- Quota circuits open immediately for primary providers; the Gemini key pool may rotate within the current request before its model circuit opens.
