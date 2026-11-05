# Autonomous run progress

## Tasks done

- Already complete before this run: API URL prefix check (`bc0c3b9`); preview ffmpeg cap and fallback (`90bbb1d`); Range support (`ea6a5a7`); shared source transcription; timing recovery across ten languages (`7ad4dbd`).
- Task 1, LLM provider swap: `20eb6a5` (pushed). Backend suite passed; frontend build passed.
- Task 2, Timing and sync, all ten languages: `aeddca0` (pushed). Backend suite (`python -m pytest tests -q`) passed; frontend build passed.
- Task 3, Dubbed playback: `3c3b4c4` (pushed). Backend suite (`python -m pytest tests -q`) passed; frontend build passed.
- Task 4, Terms and edit persistence: `d063b83` (pushed). Backend suite (`python -m pytest tests -q`) passed; frontend build passed.
- Task 5, Attempt history: `585d67f` (pushed). Backend suite (`python -m pytest tests -q`) passed; frontend build passed.
- Task 6, Editor UX: `96d5902` (pushed). Backend suite (`python -m pytest tests -q`) passed; frontend build passed.

## Current task

- All six requested tasks are complete.

## Next task

- Final verification and deployment environment inventory.

## Decisions

- Preserve all user-facing model-name removal.
- Do not print environment variable values.
- The installed Sarvam SDK does not expose the requested 30B chat model; use the supported 105B conversations endpoint for rewrite/QA.
- Keep user-facing provider/model names hidden. Keep the existing fallback chain with Gemini last.
- Quota circuits open immediately for primary providers; the Gemini key pool may rotate within the current request before its model circuit opens.
- Task 2 uses detected source silence for safe speech windows; 100ms tolerance is accepted only when it fits within that silence, preserving the no-overlap invariant. Speed-up is capped at 15%, and export duration tolerance is 50ms.
- Task 3 uses the project `/media` MP4 for the main Dubbed toggle; the side-by-side Dubbed player is unmuted by default. Withheld status is shown alongside its clip time range.
- Task 4 term rules default to project scope. The explicit “Save this rule to global term memory” option copies it to future projects. Protected spelling remains the canonical displayed term; language-specific spoken forms are used only for synthesis.
- Task 5 stores attempt WAVs under the project's export directory and serves them through a clip-scoped endpoint. The first take has no previous audio; subsequent retries carry both versions.
- Task 6 keeps attempts collapsed by default. Clicking a clip plays its timed range from the mixed dub, and the single-player mode is the default.
- Current task: final verification and environment inventory.
