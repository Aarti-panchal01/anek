# Autonomous run progress

## Tasks done

- Already complete before this run: API URL prefix check (`bc0c3b9`); preview ffmpeg cap and fallback (`90bbb1d`); Range support (`ea6a5a7`); shared source transcription; timing recovery across ten languages (`7ad4dbd`).
- Task 1, LLM provider swap: `20eb6a5` (pushed). Backend suite passed; frontend build passed.
- Task 2, Timing and sync, all ten languages: `aeddca0` (pushed). Backend suite (`python -m pytest tests -q`) passed; frontend build passed.
- Task 3, Dubbed playback: `3c3b4c4` (pushed). Backend suite (`python -m pytest tests -q`) passed; frontend build passed.

## Current task

- Task 4: Terms and edit persistence.

## Next task

- Task 5: Attempt history.

## Decisions

- Preserve all user-facing model-name removal.
- Do not print environment variable values.
- The installed Sarvam SDK does not expose the requested 30B chat model; use the supported 105B conversations endpoint for rewrite/QA.
- Keep user-facing provider/model names hidden. Keep the existing fallback chain with Gemini last.
- Quota circuits open immediately for primary providers; the Gemini key pool may rotate within the current request before its model circuit opens.
- Task 2 uses detected source silence for safe speech windows; 100ms tolerance is accepted only when it fits within that silence, preserving the no-overlap invariant. Speed-up is capped at 15%, and export duration tolerance is 50ms.
- Task 3 uses the project `/media` MP4 for the main Dubbed toggle; the side-by-side Dubbed player is unmuted by default. Withheld status is shown alongside its clip time range.
- Current task: Task 4. Next task: Task 5, Attempt history.
