# SPEC.md: Anek, a terminology-aware, self-correcting dubbing studio

Name: **Anek** ("many"). Tagline: **"Speak once. India understands."** Hero headline: **"Say it once. Mean it everywhere."** Do not use "One X. Every Indian language." style lines; that is Sarvam Studio's tagline. Set in `APP_NAME` (config). Before launch, search app stores, domains, GitHub and trademark registries for clashes.

Built for Gnani AI's **Great Indian AI Internship Challenge**. Category: **Creator** (and, in practice, anyone who teaches or informs in one language).
Read this entire file before writing any code. Follow it phase by phase (Section 14).

---

## 0. How to work (rules for the coding agent)

1. **Use only documented Gnani endpoints.** Before writing any Gnani call, open the docs page named in Section 4 and copy the request shape from it. Never invent endpoints, fields or model names. If a doc page is unreachable or ambiguous, stop and ask the human.
2. **Secrets:** `GNANI_API_KEY` and the LLM key come from environment variables only (`.env`, git-ignored). Provide `.env.example`. Never log keys or voice embeddings.
3. **Tests use mocks.** Every Gnani and LLM call goes through a client class with a mock implementation (`GNANI_MOCK=1`, `LLM_MOCK=1`). Do not spend live credits in automated tests.
4. **Run autonomously, phase by phase.** Finish a phase, run its tests, commit with a clear message, print a short summary, and continue straight into the next phase without asking for permission. Stop and ask the human ONLY when: (a) a live Gnani/LLM key is missing or invalid, (b) the human must listen to audio (end of Phase 1, the cloning gate), (c) a Gnani doc page is unreachable or ambiguous (rule 1), (d) a test keeps failing after 3 honest fix attempts, or (e) the next step would spend more than `MAX_CREDITS_PER_JOB` credits. Never skip a phase's "done when" to move faster.
5. **No copying.** Do not copy code, UI assets, branding or private endpoints from any third-party product or from other participants' repos. Do not reverse engineer any product.
6. **Synthetic data only.** Test clips use fictional brands (e.g. "Zorvo") and fake phone numbers. No real people's data.
7. **Voice safety:** only clone a voice after a live consent recording (Section 8.2). Label every export "AI dubbed". Provide delete for voices, uploads and projects.
8. **Explainable code.** One student must be able to explain every module in an interview. Prefer small functions, plain Python, comments that say *why*.
9. **Never hide failures.** A clip that cannot be fixed is flagged for a human with a reason. Do not silently swallow errors or silently "fix" numbers.

---

## 0a. Prerequisites (the human does these before Phase 0)

- [ ] **Gnani API key.** Dashboard at app.gnani.ai, API Keys, Add API Key (max 3 keys). Put it in `.env` as `GNANI_API_KEY`. The dashboard showed 5,000 / 5,000 credits.
- [ ] **LLM key** (any OpenAI-compatible endpoint). Cheapest working options: Groq with an open-weight model such as `openai/gpt-oss-120b` (base URL `https://api.groq.com/openai/v1`), or Gemini via its OpenAI-compatible endpoint. Check the provider's current free-tier limits and model names before coding. Put it in `.env` as `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`. A Claude.ai chat plan is NOT an API key.
- [ ] **ffmpeg and ffprobe** on PATH (`ffmpeg -version` works), Python 3.11+, Node 20+.
- [ ] **One clean voice sample**, 20-30 s, own voice, quiet room, WAV or MP3. Save as `samples/my_voice.wav` (git-ignored).
- [ ] **Later (Phase 3 and 6):** a 60 s test video, then ~10 eval clips (Section 12).

---

## 1. What we are building (one paragraph)

A web application where a creator uploads a short video (**hard cap: 2 minutes**, enforced server-side with ffprobe; see Section 2a), chooses one or more target languages from Gnani's 10 supported languages, and clones their own voice. The app produces a dubbed version in each language that **speaks in the creator's own voice**. Unlike a plain translate-and-speak pipeline, Anek has an **intelligence layer ("the Brain")** that (a) builds a **term sheet** so names, brands, numbers and domain terms are handled correctly instead of mistranslated, (b) translates for **meaning, not literal words**, (c) **independently verifies** every clip, including a round-trip check on the generated audio using Gnani Prisma, and (d) **auto-corrects** failures with a bounded retry loop, always showing what it changed. A review studio shows the video on the left and the clips on the right, where the creator can edit any translation, regenerate a clip's audio, and see why each clip is flagged green, yellow or red. The exported dub ships with a verification report.

**Core promise:** "Dubbed in your own voice, with every number and term checked, so people who do not speak your language can still learn from you."

---

## 1a. Who Anek is for (audience and use cases)

The problem is not "dubbing for YouTubers". It is that useful knowledge is trapped in one language. A physics lecture in English does not reach a Kannada-medium student; a health or money explainer in Hindi does not reach a Tamil-speaking family. Anek lets one person speak once and be understood in 10 Indian languages, in their own voice.

| Use case | Example | What Anek must get right |
|---|---|---|
| Teaching and lectures | English physics lecture for Hindi or Kannada students | Technical terms (keep in English or give the approved local term), units and formulas (`9.8 m/s²`), numbers |
| Informative explainers | Hindi video on a scheme, health tip or money topic, for Tamil viewers | Names, amounts, dates, plain-language meaning |
| Podcasts and talks | A single-host talk or monologue | Natural register, long-form consistency |
| Sports commentary | Cricket commentary in English or Hindi | Player names, scores and overs (`45 runs off 23 balls`, `3/45`), excitement, idioms |
| Business and training | Product demos, onboarding, how-to | Brand names, prices, phone numbers |

**Honest fit in v1** (state this in the README and the UI):
- Works well: single-speaker, speech-led video (lectures, explainers, talks, tutorials, single-host podcasts).
- Works with limits: commentary. Numbers, names and excitement are handled, but the crowd and stadium sound is lost because the original audio is replaced (see 8.4).
- Not yet: multi-speaker podcasts and interviews (needs diarization, Section 16), and music-heavy video.

**Content type** (Advanced options on the Create-a-dub screen, Section 2a): Lecture / Education, Explainer / News, Podcast / Talk, Sports commentary, Business / Marketing. It sets defaults only, and the user can override everything:
- *Lecture / Education:* tone = clear, patient teacher; technical terms default to `keep` in Latin script (students hear English terms in class), with an option to switch to approved local terms; units and formulas protected.
- *Explainer / News:* tone = neutral, plain language; amounts, dates and names protected.
- *Podcast / Talk:* tone = conversational; idioms are rendered by meaning.
- *Sports commentary:* tone = energetic, short sentences; player and team names `transliterate`; scores, overs and statistics protected.
- *Business / Marketing:* tone = confident; brand names `keep`; prices and phone numbers protected.

---

## 1b. Positioning (what makes Anek different)

Sarvam Studio already offers voice-preserving dubbing, line-by-line review, tone presets (Auto, Formal, Urban colloquial), lip-sync, multi-speaker, long files (up to 1 hour), SRT export and an API. Competing on "dub into Indian languages in your own voice" loses. Anek competes on **trust**:

| Sarvam Studio | Anek |
|---|---|
| You review every line yourself | The Brain checks every line first and flags only what is wrong, with reasons |
| Tone presets | Term sheet: names, brands, units and numbers approved before the expensive step |
| Export the dub | Export the dub plus a verification report (round-trip STT check on the generated audio) |
| Broad (film, drama, multi-speaker) | Narrow and honest: single-speaker teaching, explainers, talks, in v1 |

Never claim lip-sync, multi-speaker or long-file support. Lead every page with verification, not with "many languages".

---

## 2. End-to-end user flow

1. **Setup screen**
   - Upload video (mp4/mov/webm) or record in the browser. Reject anything over 2 minutes with a clear message. Show duration and an estimated credit cost before continuing.
   - Source language: choose, or "auto-detect" (Prisma v3.0 `indic-auto`, see Section 4; verify).
   - Target languages: multi-select from the 10 supported.
   - Glossary (optional): protected terms the creator already knows (brand and product names).
2. **Voice screen**
   - The app shows a random consent phrase. The user records it live in the browser. Prisma transcribes it; it must match the phrase (fuzzy match) or cloning is blocked. This confirms a live recording; it does not prove identity, and the UI must say so honestly.
   - The user records 20-30 seconds of clean speech. The server creates a speaker embedding (Section 4) and stores it server-side, linked to the user session.
   - Buttons: "Preview my voice" (speaks a short test sentence), "Delete my voice".
3. **Processing screen:** one progress row per language with stage labels: transcribing, building term sheet, translating, speaking, verifying, fixing. Progress arrives by server-sent events.
4. **Term sheet review (before the expensive step):** after the term sheet is built, show it for approval or edit (Section 9.2). Then the pipeline continues to translate, speak and verify.
5. **Review studio**
   - Left: video player with an Original / Dubbed toggle and language tabs. **Compare mode** (see Section 11c) plays original and dub together.
   - Right: list of clips (5-15 s each). Each clip row shows time range, source text, editable translation, play original, play dub, status badge (green/yellow/red) with reasons, Regenerate, Revert, and an attempt history disclosure.
   - Top bar: counts of red/yellow/green for the current language, "next problem" button, a Term sheet drawer, and Find and Replace.
6. **Export:** per language: mp4 (dubbed audio muxed), SRT subtitles, audio-only, verification report (JSON and a readable page). All labeled "AI dubbed".

---

## 2a. Create-a-dub screen (setup, modelled on common dubbing UIs)

One modal/page, same style tokens as Section 11b. Do not copy any other product's code, text or branding.

- **Tabs:** Upload | Record (record straight from the camera/mic in the browser; same 2-minute cap and a visible timer).
- **Preview:** the video with a play button, file name chip and duration. Show "over 2 min" as an error and disable Create.
- **Source language:** dropdown with search. First item "Auto-detect" (Prisma v3.0 `indic-auto`). Then only the 10 Gnani languages. No Odia or Assamese.
- **Number of speakers:** fixed to "1 speaker" in v1 (disabled select, with a tooltip: "Multi-speaker is not supported yet").
- **Target languages:** multi-select dropdown with search, a select-all checkbox and removable chips. Only `ENABLED_LANGS`.
- **Advanced options (collapsed by default):**
  - **Voice:** `Clone my voice` (default; needs the consent flow in 8.2) or `Use a stock voice` (Gnani Timbre v3.0 voices from the docs; all 13 speak all 10 languages). The two paths use different models; name them honestly in the report.
  - **Content type:** Lecture / Education, Explainer / News, Podcast / Talk, Sports commentary, Business / Marketing (Section 1a). Sets default tone and term treatments.
  - **Translation style:** Neutral (default), Formal, Colloquial (everyday spoken), Marketing/energetic. Passed to the translator as `tone_hint`; overrides the content-type tone if the user picks one.
  - **Translation model:** read-only label showing the LLM actually used (`LLM_MODEL`).
  - **Glossary:** optional protected terms (Section 2 step 1).
- **Cost card:** "Estimated credits: N (x languages)" instead of any "free" claim. Show remaining credits.
- Buttons: Cancel, Create dub (disabled until upload, source, at least one target and, for cloning, a consented voice).

---

## 3. End-to-end execution (what runs, in order)

For each project and each target language:

```
video
 -> [extract]   ffmpeg: 16 kHz mono wav for analysis
 -> [chunk]     split at silences into clips (5-15 s target, hard max 25 s)
 -> [stt]       Prisma transcribes each clip in the source language
 -> [termsheet] Brain scans the full transcript once -> term sheet (user approves)
 -> [translate] Brain translates each clip with term sheet + neighbor context + duration budget
 -> [check-text] deterministic + model checks on the text; failing clips go to auto-correct (<=2 retries)
 -> [synth]     cloned-voice speech per clip (Gnani voice cloning)
 -> [fit]       measure duration; shorten text or mildly time-stretch to fit the slot
 -> [verify-audio] Prisma transcribes the dubbed clip; compare numbers/terms/similarity
 -> [flag]      green / yellow / red with reasons; auto-correct once more if fixable
 -> [mix]       place clips on a timeline, mux with the original video
 -> [export]    mp4, SRT, audio, report
```

Every stage writes its result per clip to the database so a crash resumes where it stopped. Audio is cached by hash of (text, voice id, language) so nothing is generated twice.

---

## 4. Gnani facts (verified from Gnani's public docs; re-check each page before coding)

Base URL: `https://api.vachana.ai`. Auth header on every request: `X-API-Key-ID: <key>`.

**Languages (10):** Bengali `bn-IN`, English `en-IN`, Gujarati `gu-IN`, Hindi `hi-IN`, Kannada `kn-IN`, Malayalam `ml-IN`, Marathi `mr-IN`, Punjabi `pa-IN`, Tamil `ta-IN`, Telugu `te-IN`. Source: https://docs.gnani.ai/api/STT/languages.md. Odia and Assamese are NOT supported; do not offer them.

**Speech-to-text (Prisma):**
- REST: `POST /stt/v3` (the `3` is the URL version, not the model version). Model chosen by the `model` value: `prisma-v2.5` (default) or `prisma-v3.0`.
- Prisma v3.0: language detection with `language_code=indic-auto` (reply carries `detected_language_code`), mixed-language speech, context hints (names/topics), romanised output option. Prisma v2.5: monolingual per language, number formatting, word boosting, find-and-replace.
- REST audio length limit: docs disagree (30 s on one page, 60 s on another). **Chunk at 25 s or less.** With the 2-minute cap, **Batch STT is not used**: every clip goes through REST. (This also avoids Batch not supporting Gujarati and Punjabi.)
- Exact request fields: read https://docs.gnani.ai/api/STT/speech-to-text.md and https://docs.gnani.ai/api/STT/models/prisma-v3.md. **Do not guess them.**
- **Verification step (9.6):** Prisma v2.5 returns numbers as spoken words except for `hi-IN` and `en-IN`. Prisma v3.0 with `format=transcribe` writes digits itself, so use `VERIFY_STT_MODEL=prisma-v3.0` for audio verification and keep v2.5 only as a fallback (then fall back to the model-based number-word check). v3.0 also accepts up to 200 `keyterms` phrases for term hints; v2.5 accepts only single-word boost terms.
- Challenge pages name Prisma v2.5. v3.0 is in the docs. VERIFY that v3.0 works with the programme credits before depending on it; make the model a config value.

**Seen in the Gnani dashboard (app.gnani.ai), confirmed on 3 Nov 2026:**
- Prisma V3.0 is listed as a real option: "Multilingual ASR for 10 languages with automatic language identification and 4 output styles" (Output Style dropdown, default `Transcribe`; it controls how English words and numbers are written). Prisma V2.5 is listed as "a dedicated model for each Indian language, built for call-centre and telephony audio". So V3.0 is available on the programme account; Phase 1 still confirms it works through the REST API.
- **Accuracy hints:** the playground offers Keyterms (names, brands, terms to listen for) and Names, and says selecting one disables the other. Our term sheet should feed **Keyterms** to Prisma. Read the docs page for the exact API field names; do not guess.
- Playground TTS model: **Gnani Timbre v3.0** (stock voices, audio tags such as `<excited>`, 800-character limit). Stock voices are NOT used in v1 (we dub in the creator's own cloned voice); keep Timbre only as an optional fallback or for the voice-preview sentence.
- Voice Cloning page is marked **Beta** and has a language selector; Hindi was shown. Treat every language as unproven until the cloning gate passes.
- The dashboard playgrounds (Speech to Text, Text to Speech, Voice Cloning) can be used by the human to sanity-check a language by ear before or during Phase 1. They spend the same credits.

**Voice cloning (two steps):**
1. `POST /api/v1/tts/voice-clone/embeddings` multipart, field `audio_file` (WAV/MP3, 5-30 s clean speech) returns `{ "embedding": "...", "shape": [1,768], "dtype": "torch.bfloat16" }`.
2. `POST /api/v1/tts/inference` JSON body: `text`, `model: "vachana-vc-v1"`, `audio_config` (REQUIRED: sample_rate 8000-44100, num_channels, sample_width, encoding `linear_pcm`|`oggopus`, container `raw`|`mp3`|`wav`|`mulaw`|`ogg`), `speaker_embedding` (an OBJECT with `embedding`, `shape`, `dtype`, not a plain string). Returns binary audio. There is **no language field**: the language comes from the script of `text`.
- Streaming variants exist (SSE, WebSocket) but cloned speech is not needed in streaming for this product. Use REST.
- Docs: https://docs.gnani.ai/api/VC/vc-inference.md and https://docs.gnani.ai/api/VC/voice-clone-embeddings.md
- Maximum text length per call for the API is not confirmed (the playground shows 800 characters). **Keep each synthesis call under 300 characters.**

**Not offered by Gnani (as far as the docs show):** machine translation and a dubbing API. Translation uses an LLM through an OpenAI-compatible API (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`). Gnani Evon (open-weight reasoning model) is the preferred brain if a hosted endpoint is available; otherwise use another open-weight model. **The README must name the LLM actually used.**

**Cloning model naming:** the docs page for cloning has mentioned "Timbre v2.0" in places while v2.0 is deprecated. In the submission, name the component as "Gnani voice cloning (`vachana-vc-v1`)" and do not claim a Timbre version for it unless the docs say so.

**Credits:** the programme gives 5,000 credits. The cost per call is not documented here. Phase 1 measures it (Section 14).

---

## 5. Tech stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (via SQLAlchemy or plain sqlite3), `httpx`, `pydantic`, `rapidfuzz` (similarity), `pytest`.
- **Audio/video:** `ffmpeg` and `ffprobe` invoked with argument lists (never through a shell string). Silence detection with ffmpeg `silencedetect`.
- **Jobs:** an in-process async worker reading a `jobs` table (no Redis). Concurrency limits per external service (semaphores). Retry with exponential backoff on 429/503.
- **Frontend:** React + Vite + TypeScript, plain CSS. Built and served as static files by FastAPI. Server-sent events for progress.
- **Storage:** local disk under `data/` (uploads, clips, audio, exports), SQLite file `data/anek.db`.
- **LLM:** any OpenAI-compatible chat endpoint through one `clients/llm.py` class. A free-tier open-weight model on Groq is a known-working choice for similar projects, but verify the current model name and limits first. Use JSON-mode or strict JSON prompting for the term sheet and checks, and validate every response with pydantic.
- **Deploy:** Dockerfile (ffmpeg included). Host must support long-running processes (not Vercel or Netlify). Render free tier works but sleeps after idle; wake it before demos.

---

## 6. Repository layout

```
anek/
  SPEC.md
  README.md                 # what it is, models used, how to run, evaluation results
  .env.example
  Dockerfile
  backend/
    app.py                  # FastAPI app, routes, SSE
    config.py               # env, thresholds, language table
    db.py                   # schema + queries
    jobs.py                 # worker, job states, resume logic
    clients/
      gnani.py              # stt(), make_embedding(), synth(); real + mock
      llm.py                # chat_json(); real + mock
    pipeline/
      extract.py chunk.py stt_step.py
      mix.py export.py fit.py
    brain/
      termsheet.py translate.py
      checks.py             # deterministic checks (numbers, terms, script, length)
      judge.py              # model-based meaning check (advisory)
      autocorrect.py        # bounded retry loop
      numbers.py            # numeral normalization, all 10 scripts
      prompts.py            # all prompt templates in one place
    verify/
      audio_verify.py       # Prisma round trip on dubbed audio
      flags.py              # green/yellow/red decision
    eval/
      fixtures/             # seeded-error clips + ground truth json
      run_eval.py           # produces the evaluation report
  frontend/                 # React app (Setup, Voice, Progress, TermSheet, Review, Export)
  tests/
  data/                     # git-ignored
```

---

## 7. Data model (SQLite)

- `projects(id, name, source_lang, target_langs_json, video_path, duration_s, status, created_at)`
- `voices(id, session_id, label, embedding_json, consent_phrase, consent_ok, created_at)` (embedding stays server-side; deleted on request)
- `clips(id, project_id, idx, start_s, end_s, src_text, src_audio_path)`
- `terms(id, project_id, term, type, treatment, locked, targets_json, source)` (`type`: brand|person|place|number|unit|jargon|idiom; `treatment`: keep|transliterate|translate)
- `dubs(id, clip_id, lang, translation, status, reasons_json, attempts_json, audio_path, audio_hash, duration_s, human_edited, updated_at)` (`status`: pending|green|yellow|red)
- `jobs(id, project_id, kind, lang, clip_id, state, error, attempts, created_at)`
- `credit_ledger(id, project_id, kind, units, est_credits, created_at)`

Per-clip `attempts_json` records every auto-correction: `[{n, trigger_checks, old_text, new_text, result}]`. Never delete it. It feeds the diff view and the report.

---

## 8. Pipeline details

### 8.1 Extract and chunk
- Extract audio with ffmpeg to 16 kHz mono wav.
- Chunk at silences: run `silencedetect`, then choose cut points so clips are 5-15 s where possible, minimum 2 s, hard maximum 25 s. Prefer the longest silence near the target length. Store `start_s`/`end_s`. Do not rely on Prisma returning timestamps.
- If the video has no usable silence, cut at 12 s and mark clips `cut_mid_sentence=true` for the translator.
- Multiple speakers: v1 assumes one speaker. Warn the user in the UI if speech overlap is suspected; do not claim diarization.

### 8.2 Voice setup (consent)
- `GET /api/voices/consent-phrase` returns a random phrase from a small list (in English, or in the user's language).
- User records it. Transcribe with Prisma, compare with the phrase using fuzzy matching (`rapidfuzz`, threshold in config). Below threshold: block cloning and show why.
- Then reference audio 20-30 s: create the embedding, store it. Never expose the embedding to the browser.

### 8.3 Transcription
- Transcribe each clip with the configured Prisma model. If the source language is "auto" and the model is v3.0, use `indic-auto` and store `detected_language_code` per project (majority vote).
- Pass the user's glossary or the approved term sheet as context hints when the docs support it for that model (read the Prisma v3.0 page).

### 8.4 Synthesis, fit and mix
- Synthesize with cloned voice, REST, WAV, one call per clip (text under 300 chars; split a long clip at a sentence end).
- Measure real duration with `ffprobe`. Calibrate characters-per-second per language from the first measured outputs and store in config for the estimator.
- **Fit loop:** if duration > slot x 1.15: ask the Brain to shorten the translation (counts toward the retry cap), then re-synthesize. If still long: time-stretch with ffmpeg `atempo` at most 1.15x. If still longer than the slot: flag YELLOW "overruns slot". If much shorter: leave silence.
- Trim leading/trailing silence from synthesized clips.
- **Mix (v1):** create a silent track the length of the video; place each clip at `start_s` with `adelay`; replace the original audio entirely. State this plainly in the UI ("original background sound is not kept"). Background-music preservation is a stretch goal (Section 16).

### 8.6 Export (must be correct, not just present)

Export always renders from the **current** edited state, never from stale audio.

- **mp4:** original video stream copied (no re-encode of video where possible), dubbed audio encoded as AAC 192k stereo (or mono if the source is mono), `-movflags +faststart` for web playback. Keep the original resolution and frame rate. Container metadata `comment="AI dubbed"` and `title` set.
- **Visible label:** optional "AI dubbed" badge burned into the corner, ON by default, toggle in the export dialog. The report and file name (`<name>_<lang>_ai-dubbed.mp4`) always carry the label.
- **Audio-only:** WAV (lossless) and MP3 (192k).
- **SRT:** timings from clip `start_s`/`end_s`, UTF-8 with BOM-free encoding, lines wrapped at ~42 characters, max 2 lines per cue. Also offer the source-language SRT.
- **Report:** JSON plus a readable HTML page with per-clip status, reasons, auto-correct attempts, human edits, models used and credits spent.
- **Post-export self-check (ffprobe):** exactly one video and one audio stream; output duration within 0.1 s of the source; audio not silent (mean volume above a floor); SRT cue count equals clip count. If any check fails, show the failure instead of offering the download.
- **Stale warning:** if any clip was edited after the last export, the export button says "Re-export (3 clips changed)".
- **Red clips:** exporting with unresolved RED clips needs an explicit confirmation listing them; the report records that the user exported anyway.

### 8.5 Credit control
- Before starting, show an estimate (characters to synthesize x measured credit rate x number of languages).
- Enforce `MAX_CREDITS_PER_JOB` and `GNANI_DAILY_LIMIT` (env). Stop with a clear message when reached.
- Cache everything by hash; never re-synthesize identical (text, voice, language).

---

## 9. The Brain (intelligence layer)

All Brain functions are plain Python modules with JSON in and JSON out. They are testable **without any audio or UI** using fixtures. All LLM calls request JSON only, validate with pydantic, and retry once on malformed JSON.

### 9.1 Principles
- Deterministic checks outrank model opinions. A model never overrides a failed numeric or term check.
- Never auto-change numbers, currency, dates or locked terms by "rewriting around them"; the only permitted fix is re-translating with the failure stated, and the result must pass the same checks.
- Every change is logged in `attempts_json` and shown to the human. The human has the last word. Human edits set `human_edited=true` and are never overwritten by auto-correct.
- Max 2 auto-correct retries per clip per cause. After that: flag with a reason.

### 9.2 Stage A: Term sheet (one pass over the whole transcript)
Input: all clips' `src_text` in order, source language, user glossary.
Output (JSON):
```json
{
  "terms": [
    {
      "term": "Zorvo",
      "type": "brand",
      "treatment": "keep",
      "targets": {"ta-IN": "Zorvo", "hi-IN": "Zorvo"},
      "locked": true,
      "note": "invented brand, do not translate"
    }
  ],
  "idioms": [
    {"phrase": "this deal is a steal", "meaning": "an unusually good price", "clip_ids": [4]}
  ]
}
```
Rules for the prompt: list names, brands, products, people, places, units, numbers with currency, domain jargon, and idioms/figurative phrases whose literal translation would change the meaning. For each, choose a treatment: `keep` (leave as in source), `transliterate` (write in the target script; provide the form per target language), or `translate` (approved target term). Locked terms are non-negotiable downstream.
The user can edit, add and lock terms in the UI. This approved sheet is saved per project. Optional: save a per-user "term memory" so approved terms are suggested in future projects.

### 9.3 Stage B: Translate a clip
Input: `{clip_text, prev_clip_text, next_clip_text, source_lang, target_lang, tone_hint, term_sheet_subset, idioms_in_clip, max_chars}`.
Output: `{"translation": "...", "terms_used": ["Zorvo"], "notes": ""}`.
Prompt rules:
- Translate **meaning and register**, not word-for-word. For idioms, express the meaning naturally in the target language.
- Obey the term sheet exactly (keep / transliterate / approved term).
- Preserve every number, currency amount, date, phone number and unit exactly (digits may be written in the target script's numerals OR ASCII digits, as set in config `NUMERAL_STYLE`).
- Keep code-mixed English words that the term sheet says to keep.
- Stay under `max_chars` (derived from slot duration x calibrated chars-per-second). Be concise, not lossy.
- Output only the JSON.

### 9.4 Stage C: Checks on text (`checks.py`, `judge.py`)
**Deterministic (authoritative):**
1. **Numbers:** extract numerals from source and translation after normalizing all digit scripts to ASCII (Devanagari, Bengali, Gujarati, Gurmukhi, Tamil, Telugu, Kannada, Malayalam), removing separators (`1,00,000`, `1,000`), and treating currency symbols/words (`₹`, `Rs`, `रुपये`, etc.) as attached to the amount. The multiset of numbers must match. Phone numbers are compared as digit strings. Decimals (`9.8`), fractions, scores written `3/45`, percentages and numbers attached to units (`m/s²`, `km`, `kg`, `overs`) are single tokens: the number and its unit must both survive. Unit tests required for each script and for these formats.
2. **Terms:** `keep` terms must appear verbatim; `transliterate` terms must match one of the approved target forms; `translate` terms must contain the approved target term.
3. **Script:** the translation must be mostly in the target script (allowing Latin for kept terms and digits).
4. **Length:** estimated duration (chars / calibrated cps) must fit the slot with tolerance.
Each failing check returns `{check, detail}` (e.g. `{check: "number_missing", detail: "499"}`).
**Model-based (advisory only):** `judge.py` back-translates the translation to the source language using a separate prompt (ideally a different model) and compares meaning with the source. Output `{meaning_preserved: bool, issues: []}`. A disagreement can only raise YELLOW, never override a deterministic pass or fail. Spoken number words ("four hundred ninety-nine") are hard to compare deterministically; use the model-based check as a secondary signal for those and rely on digits where Prisma returns them.

### 9.5 Stage D: Auto-correct (`autocorrect.py`)
```
for attempt in 1..2:
    failures = run_checks(clip)
    if no failures: break
    new_text = translate(clip, extra_instruction=describe(failures))   # states exactly what failed
    log attempt (trigger checks, old, new)
    if new_text passes all deterministic checks: accept; break
if still failing: status = RED with reasons
if passed after >=1 retry: status = YELLOW "auto-corrected, please review" (green only if no retry was needed)
```
Locked terms and numbers must come out exactly as required; otherwise the attempt fails.

### 9.6 Audio verification (`audio_verify.py`)
After synthesis, transcribe the dubbed clip with Prisma using `language_code` of the **target** language, normalize the same way, then compare:
- numbers and phone numbers: must match the translation's numbers (mismatch -> RED, possibly a synthesis problem)
- protected terms: expected target form present (fuzzy, since transliteration varies) -> else YELLOW
- overall text similarity (character-level ratio) below threshold -> YELLOW
A failed audio check triggers one regenerate (new synthesis of the same text) before flagging, because synthesis can vary.

### 9.7 Flag decision (`flags.py`)
- **RED:** any unresolved deterministic failure (number/term), or audio number mismatch after regenerate.
- **YELLOW:** auto-corrected, overruns slot, low similarity, judge disagrees, term mismatch in audio only, or time-stretched near the limit.
- **GREEN:** all checks passed with no retries.
All thresholds are config values, not hard-coded. Every flag carries a human-readable reason list.

### 9.8 Edits and term memory
- Editing a translation re-runs deterministic checks instantly (no audio cost). Regenerate then re-synthesizes and re-verifies audio.
- Editing a term in the term sheet or in a clip shows "N other clips use this term; apply to all?" with a preview diff before applying. Applied changes re-run the checks for those clips.

### 9.9 Metrics (what we measure and publish)
Using the evaluation fixtures (Section 12):
- **Caught:** of deliberately wrong translations (wrong price, dropped number, translated brand, wrong phone digit), how many did the checker flag? (checker recall)
- **False alarms:** of correct translations, how many were wrongly flagged?
- **Fixed:** of flagged clips, how many did auto-correct make correct (verified against ground truth)?
- **Broken:** how many auto-corrections introduced a new error?
Report all four per language. Publish misses too. Never claim accuracy that was not measured.

---

## 10. HTTP API

```
POST   /api/projects                       {name, source_lang|"auto", target_langs[]}
PUT    /api/projects/{id}/video            upload video (multipart; rejected if > MAX_VIDEO_SECONDS)
GET    /api/projects/{id}/estimate         duration, clip count, estimated credits
GET    /api/voices/consent-phrase
POST   /api/voices                         {consent_audio, reference_audio, label}
DELETE /api/voices/{id}
POST   /api/projects/{id}/start            {voice_id}
GET    /api/projects/{id}/events           SSE progress events
GET    /api/projects/{id}/termsheet        ;  PUT to approve/edit
POST   /api/projects/{id}/terms/apply      {term_id, replacement, preview:true|false}
GET    /api/projects/{id}/clips?lang=xx-IN
PATCH  /api/clips/{id}/dubs/{lang}         {translation}   (re-runs text checks)
POST   /api/clips/{id}/dubs/{lang}/regenerate
POST   /api/clips/{id}/dubs/{lang}/revert
POST   /api/projects/{id}/export           {lang, formats:["mp4","srt","audio","report"]}
GET    /api/projects/{id}/report?lang=xx-IN
DELETE /api/projects/{id}
```
SSE event shape: `{project_id, lang, stage, done, total, message}`.

---

## 11. UI requirements

- Review layout: video left, clip list right, language tabs on top, status counts, "next problem" button.
- Each clip row: time range, source text (read-only), translation (editable textarea with live deterministic checks), play original, play dub, status badge with reasons, Regenerate, Revert, attempt history (shows what the Brain changed and why).
- Term sheet drawer: table of terms with treatment, per-language target forms, lock toggle, "apply to all matching clips" with preview.
- Language tabs show an honest status label per language: `native-speaker checked` (only if a human marked it in the eval notes) or `machine-checked only`.
- Always visible: "AI dubbed" label on dubbed playback and exports.
- Clear, specific error messages ("3 clips failed: rate limit, retry?").
- Accessible basics: keyboard focus, labels. No decorative animation that hurts performance.

---

## 11b. Visual style: modelled on the Gnani developer dashboard (app.gnani.ai)

Goal: the studio should feel like it belongs next to Gnani's own dashboard: same calm, light, rounded, minimal look. This is a **style match, not a copy**.

**Rules**
- Write all CSS and components ourselves. Do NOT copy Gnani's source code, CSS files, JavaScript, images, illustrations or logo. Icons: use an open icon set (e.g. lucide-react).
- Do not use Gnani's logo or imply the product is made or endorsed by Gnani. The product has its own name (`APP_NAME` in config). Show the credit line "Built on Gnani AI (Prisma, voice cloning)" in the footer/About.
- Exact values (colors, font, radii, spacing) must be measured by the human in the browser: open app.gnani.ai, use DevTools (Inspect > Computed styles, eyedropper) and write them into `frontend/src/styles/tokens.css`. Until then use the approximate placeholders below. Do not claim pixel-exactness.

**Source of these values:** eyeballed from screenshots of the public gnani.ai homepage and the app.gnani.ai dashboard; the stylesheet itself could not be read. Treat every value as approximate until measured with DevTools.

**Design tokens (approximate placeholders; replace with measured values)**
```css
:root {
  --bg: #ffffff;            /* main canvas */
  --bg-sidebar: #f7f7f8;    /* left rail, very light grey */
  --bg-active: #ececee;     /* active nav item */
  --border: #e5e5e8;        /* 1px hairlines */
  --text: #1a1a1c;          /* near-black */
  --text-muted: #6b6b73;    /* section labels, helper text */
  --primary: #1a1a1c;       /* primary button: near-black pill */
  --primary-text: #ffffff;
  --accent: #f08a24;        /* warm orange used sparingly (Beta pill, highlights) */
  --accent-soft: #fff4e5;
  --ok: #1f9d55;  --warn: #d98a00;  --bad: #d64545;   /* green / yellow / red flags */
  --radius-sm: 10px; --radius-md: 14px; --radius-lg: 20px; --radius-pill: 999px;
  --font: "Inter", system-ui, sans-serif;  /* UI text; replace with the measured family */
  --font-display: "Newsreader", Georgia, serif;  /* editorial headings on landing/hero only; open font, not Gnani's */
  --info: #3b3bc4;  --info-soft: #eef0ff;           /* indigo chip (approximate) */
  --g-peach: #ffd2b0; --g-sky: #bfeaf7; --g-lavender: #d9ccf5;  /* soft gradient art tints (approximate) */
}
```

**Layout (mirrors the dashboard structure)**
- **Left sidebar** (~260-300 px): app mark + name with a small dropdown caret at top; grouped nav with small uppercase muted section labels (e.g. STUDIO: Projects, New dub, Voices; CONFIGURE: Term memory, Settings; MONITOR: Usage, Reports). Active item = light grey rounded background. Bottom: a rounded "Credits left" card with a ring progress and "x / y left", then the user row (initials avatar, name, email).
- **Top bar** in the main area: page title (semi-bold) with a small outlined "Beta" pill (orange tint), and outlined rounded buttons on the right (Docs, Help).
- **Main area**: generous whitespace, content in one clean column or two panes. Review studio = video (left) + clips list (right panel with a left hairline border and a title like "Clips").
- **Right panel empty states:** a soft illustration-style placeholder plus a short sentence and one primary action.

**Components**
- Primary button: near-black, white text, large radius (nearly pill), icon + label. Secondary: light grey fill (e.g. Cancel) or white with 1px border, same radius. Disabled: light grey fill, muted text. Small "Explore"-style buttons on marketing pages: white, 1px border, ~8-10 px radius.
- Chips / scenario selectors: pill-shaped, 1px border, icon + label; selected chip has a darker border and slightly tinted fill. Use for language selection and term treatments (Keep / Transliterate / Translate).
- Dropdown selects: fully rounded (pill) with caret and 1px light border; open menus are white cards with ~20 px radius, a search field at the top, and rows with a small coloured gradient dot per language.
- Info chip: pale indigo pill with indigo text (e.g. "2 min max", credit hints). Success card: white, 1px border, with a pale green icon tile.
- Modals: ~24 px radius, title top-left, close (x) top-right, buttons bottom-right.
- Large borderless text areas for editing translations, with a small character counter (circular or "n/800") at the bottom-left.
- Status badges for clips: small rounded pills, green / yellow / red with an icon AND text (never color alone).
- Progress: thin bars or rings, calm, no flashy animation.
- Cards: white, 1px border, radius-lg, no heavy shadows.

**Quality bar**
- Light theme first. Dark mode is optional and out of scope.
- Responsive down to tablet width; the review studio may stack on narrow screens.
- Contrast at least WCAG AA; focus rings visible; every control reachable by keyboard.
- Keep motion minimal. A 2-minute video has at most ~25 clips per language, so no list virtualization is needed.

---

## 11c. Before / after (compare)

Creators must be able to judge a dub and their own edits at a glance.

1. **Video compare:** a Compare toggle on the player. Two modes: (a) *Switch*: one player, an Original/Dubbed button that keeps the playhead position and play state when toggled; (b) *Side by side*: two synced players (original, dubbed), muted except the one selected. Seeking one seeks both.
2. **Clip compare:** per clip, play original and play dub back to back ("Original then dub" button), with the time range highlighted in the player.
3. **Text compare:** per clip, three states are kept and viewable: *source text*, *Anek's draft translation* (first accepted version), and *current translation* (after your edits). A diff view highlights changed words. Revert restores Anek's draft (or the last auto-corrected version) and is itself logged.
4. **Audio compare:** after Regenerate, keep the previous audio and offer "Previous take / New take" playback until the user accepts or reverts. Cached by hash, so reverting costs no credits.
5. **Export check:** the export dialog offers a preview of the final mp4 against the original before downloading.

---

## 11d. Landing page (public, optional but recommended)

One page, same tokens. Gradient art is generated by us in CSS (blurred radial gradients in `--g-peach`, `--g-sky`, `--g-lavender`); no images or illustrations from other sites.

1. **Hero:** serif headline "Say it once. Mean it everywhere." with the tagline "Speak once. India understands." under it, one sentence of subtext ("Your voice in 10 Indian languages, with every price, number and name verified before you publish."), primary button "Create a dub", secondary "See an example".
2. **Use-case row:** five cards (Teach, Explain, Podcast, Commentary, Business) from Section 1a, each with a one-line before/after example.
3. **How it works:** Clone your voice, Translate for meaning, Verify every number, Export. Four small cards.
4. **Honest limits strip:** single speaker, up to 2 minutes, original background sound not kept (v1).
5. **Footer:** "Built on Gnani AI (Prisma, voice cloning)", AI-dubbed notice, delete-my-data link.

---

## 12. Evaluation (the proof we publish)

Create `backend/eval/fixtures/` with:
1. **Source clips** (30-60 s each, ~10 clips, our own voice, fictional brand "Zorvo") containing: prices, phone numbers, dates, a brand name, a figure-of-speech idiom, and one Hinglish phrase. Include two use-case clips written by us (no real broadcast or lecture material): a short physics-style lecture with decimals, units and a formula, and a short cricket-style commentary with fictional players, a scoreline and overs.
2. **Ground truth JSON** per clip per language: the correct numbers, the approved term forms, and a reference translation (written or verified by a native speaker where possible; mark `verified_by_native: true/false`).
3. **Corrupted variants:** programmatically create wrong translations (change a digit, drop a number, translate a keep-term, swap a phone digit, replace the price) to test the checker.
`run_eval.py` outputs `eval_report.json` and a readable `EVAL.md` with the four metrics per language, a list of misses, and credit cost. Run it for all enabled languages. Languages that fail the cloning gate (Phase 1) are disabled and listed as such.

---

## 13. Config (all in `config.py` / env)

`GNANI_API_KEY`, `GNANI_BASE_URL`, `STT_MODEL` (default `prisma-v2.5`), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `NUMERAL_STYLE` (`ascii`|`native`), `MAX_VIDEO_SECONDS` (default 120, hard cap), `APP_NAME` (`Anek`), `TRANSLATION_STYLES` and `CONTENT_TYPES` (lists in Sections 1a and 2a), `VERIFY_STT_MODEL` (default `prisma-v3.0`, fallback `prisma-v2.5`; decided in Phase 1), `MAX_CREDITS_PER_JOB`, `GNANI_DAILY_LIMIT` (hard stop for the whole app per day; protects the 5,000 programme credits on a public demo), `PER_IP_LIMITS` (voice clones, previews, jobs per hour), `FIT_TOLERANCE` (1.15), `MAX_STRETCH` (1.15), `SIMILARITY_THRESHOLD`, `CONSENT_MATCH_THRESHOLD`, `ENABLED_LANGS` (set after the cloning gate), `GNANI_MOCK`, `LLM_MOCK`.

---

## 14. Build phases (do one at a time; each has a "done when")

**If time is short, minimum demo order:** Phases 0, 1, 2, 3, then 4 and 5. Phase 6 (eval) is what proves Anek is different from plain dub tools, so do it before Phase 7 whenever possible. Never claim a language or feature in the README that the cloning gate or eval did not support.

**Phase 0: scaffold.** Repo layout, config, `.env.example`, mocks for both clients, pytest running. *Done when:* `pytest` passes with mocks and `GNANI_MOCK=1 python -m backend.app` starts.

**Phase 1: Gnani client + cloning gate (CLI, live).** Implement `clients/gnani.py` from the docs pages in Section 4. A script `scripts/cloning_gate.py` takes a reference wav and test sentences for all 10 languages, writes `out_<lang>.wav`, then transcribes each output with Prisma and compares numbers. It prints a pass/fail table and the credits used per call. *Done when:* the table exists and `ENABLED_LANGS` is set from it. This is the ONE planned stop: print the table, tell the human which output files to listen to, and wait. (The human also listens and, where possible, gets a native speaker's opinion.)

**Phase 2: the Brain, text only.** `brain/*` with fixtures and tests: numbers in all 10 scripts, term checks, term sheet extraction, translation prompt, auto-correct loop with mock LLM scripted to fail then succeed. *Done when:* tests pass and a CLI `python -m backend.brain.demo fixtures/clip1.json hi-IN` prints translation, checks, attempts and flags.

**Phase 3: one-clip audio pipeline (CLI).** Extract, chunk, STT, Brain, synth, fit, verify, mix to a short mp4. *Done when:* a 60 s clip becomes a dubbed mp4 in one language with per-clip flags printed.

**Phase 4: API, DB, jobs, resume.** All routes in Section 10, SQLite, background worker, SSE, caching, credit caps. *Done when:* killing the server mid-job and restarting resumes without redoing finished clips.

**Phase 5: Review studio UI.** Create-a-dub screen (2a), voice screen (with consent), progress, term sheet, review with compare mode (11c), export (8.6). *Done when:* a user can upload a clip of up to 2 minutes, approve terms, see flagged clips, edit one, compare before/after, regenerate, and export mp4/SRT/report with the self-check passing.

**Phase 6: all enabled languages + evaluation.** Run `run_eval.py`, write `EVAL.md`. *Done when:* the four metrics exist for every enabled language, with misses listed.

**Phase 7: harden and ship.** Delete buttons, rate limits for public demo, Dockerfile, README (what it is, models used by exact name, LLM named, each contributor's role, how to run, evaluation results, limitations). *Done when:* a fresh clone runs with the README alone and the demo link works.

---

## 15. Acceptance checklist (the product is done only when all are true)

- [ ] Upload a video, pick languages, clone own voice with a live consent phrase.
- [ ] Term sheet is built, shown for approval, and applied downstream.
- [ ] Every clip has a status with human-readable reasons; nothing is silently changed.
- [ ] Wrong price/number/term is caught by deterministic checks and by audio round trip.
- [ ] Auto-correct logs every change; the human can edit, regenerate and revert.
- [ ] Content type selection sets sensible defaults, and the UI/README state which use cases are supported and which are not (Section 1a).
- [ ] Videos over 2 minutes are rejected (server-side); Upload and Record both work.
- [ ] Create-a-dub screen has source, targets, voice, translation style and a credit estimate (Section 2a).
- [ ] Compare mode works: original vs dub, draft vs edited text, previous vs new audio (Section 11c).
- [ ] Export includes mp4, SRT, audio and a verification report, all labeled "AI dubbed", and passes the post-export self-check (Section 8.6).
- [ ] Resumable jobs, credit caps, and caches work.
- [ ] `EVAL.md` shows caught / false alarms / fixed / broken per language with misses.
- [ ] README names Prisma (model/version used), Gnani voice cloning (`vachana-vc-v1`) and the translation LLM, and lists each member's contribution.

---

## 16. Out of scope / stretch (do NOT start unless all phases are done)

Speaker diarization, lip-sync, waveform timeline editor, Odia/Assamese (not supported by Gnani), background-music separation and re-mixing (Demucs), live (real-time) dubbing, mobile app, user accounts beyond a session cookie.

---

## 17. Known risks (the agent should surface these, not hide them)

1. Cloned voice quality may vary by language. The cloning gate decides which languages ship.
2. Translation LLM quality varies by language, especially for lower-resource ones. The evaluation shows it.
3. Spoken number words are hard to verify deterministically. We rely on digits and a secondary model check, and say so.
4. Cloned speech is not instant. Even a 2-minute video takes a while across several languages; the UI must show progress and allow resume.
5. Use-case limits: multi-speaker audio and background sound (crowd, music) are not handled in v1. Say so in the UI and README rather than implying commentary or podcasts are fully supported.
6. Gnani doc inconsistencies (STT length limit; cloning model version label). Handle by config, chunk conservatively and re-check the docs.
