# Anek

Anek is a local-first, single-speaker video dubbing studio for Indian languages. It extracts speech, prepares a term sheet, translates spoken dialogue, clones the creator's voice, and flags clips for human review. Every generated file is marked AI dubbed. The app is an independent project built on Gnani AI.

## Models and services

- Speech recognition and audio round-trip verification: Gnani Prisma (`prisma-v3.0` for current verification; `prisma-v2.5` remains the configured baseline).
- Voice cloning: Gnani Vachana voice-clone embedding and inference (`vachana-vc-v1`).
- Translation, cleanup, candidate selection and meaning checks: Groq OpenAI-compatible chat completions, configured by `LLM_BASE_URL` and `LLM_MODEL` (the supplied example uses `openai/gpt-oss-120b`).
- Media processing: FFmpeg.

## Contributors

- Aarti Panchal — product owner, source material, listening review, and project direction.
- Codex — implementation, evaluation tooling, and technical documentation.

## Run locally

Requirements: Python 3.11+, Node.js 20+, npm, and FFmpeg available on `PATH`.

1. Clone this repository and enter its directory.
2. Create a virtual environment and install the project:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   ```

3. Copy `.env.example` to `.env`, then set `GNANI_API_KEY` and `GROQ_API_KEYS` (one or more comma-separated keys). Keep `.env private; it is ignored by Git. The example configures all ten language codes after the user-reviewed cloning gate; Marathi and Punjabi audio-number checks are marked low trust.
4. Install and build the web studio:

   ```powershell
   cd frontend
   npm ci
   npm run build
   cd ..
   ```

5. Start the API and studio:

   ```powershell
   python -m backend.app
   ```

   Open `http://127.0.0.1:8000`. The first run stores the SQLite database, uploads, generated exports, and LLM response cache in the ignored `data/` directory. Voice setup requires recording the displayed consent phrase live.

For a mock-only API smoke run, set `GNANI_MOCK=1` and `LLM_MOCK=1`; real media processing requires the live services.

## Docker

Build with `docker build -t anek .`, then run with a private environment file and persistent data volume:

```sh
docker run --rm -p 8000:8000 --env-file .env -v anek-data:/app/data anek
```

Do not publish a port to the public internet without a TLS-terminating reverse proxy, authentication, and suitable persistent rate-limit storage. The included per-IP limits are in-process and reset when the server restarts; multiple workers do not share counters.

## Evaluation

Run `python -m backend.eval.run_eval` to regenerate [EVAL.md](EVAL.md) and `eval_report.json`. The checked-in evaluation is an offline synthetic probe of number/brand checks and scripted correction outcomes across all enabled languages. It has no audio assets and no native-speaker verified translations, so it does not establish ASR quality, spoken naturalness, semantic fidelity, cloning quality, or real-world accuracy. See EVAL.md for per-language caught, false-alarm, fixed, broken, misses, and credit-cost results.

## Supported use and limitations

Anek is intended for short, speech-led, single-speaker explainers, lessons, tutorials, talks, and solo podcasts up to two minutes. Multi-speaker conversations, music-heavy videos, legal/medical-critical publication without expert review, and guaranteed lip synchronization are not supported. Translation, speech recognition, cloning, and automatic meaning checks can make mistakes. Yellow and red clips require human review; unclear source wording should remain unresolved instead of being guessed. Marathi and Punjabi audio-number verification is explicitly low trust. A public demo URL is not configured.

Delete a project from its review screen, or use the footer's **Delete my data** control to remove all local project data and voice references. This is irreversible.
