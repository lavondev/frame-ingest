# Architecture and design notes

The "why" behind the pipeline choices that are not obvious from the code. These notes come from
faircopy, the web app this pipeline was extracted from, and still describe the engine in
`src/frame_ingest/`. What is being built around it (CLI, agent mode, security layers) is in
[PLAN.md](PLAN.md).

## 1. Stage order: vision before correction

The pipeline transcribes first, then looks at frames, **then** corrects the transcript:

```
probe → audio → transcribe → frames → vision → correct → synthesize → assemble
```

Speech-to-text models are bad at exactly the words that matter most in a video: product names,
people, acronyms, code identifiers. But those same words are very often **on screen** (slide titles,
terminal output, UI labels, lower-thirds). The vision pass reads them verbatim, so running it first
turns the screen into a ground-truth glossary for the correction pass:

- Correction receives the user's glossary, the file name, **and the on-screen text and entities seen
  within ±10 s of each window, plus the most frequent names across the whole video.** "Widjet
  Frobnicator" in the audio is fixed because "Widget Frobnicator" is on the slide.
- The reverse dependency is weak: vision benefits from a *raw* transcript window (it explains what is
  being discussed), and a raw one is good enough for that. So there is no cycle.
- Synthesis comes last because it should see the corrected text and the scene data.

Correction is deliberately conservative, because a "helpful" rewrite is worse than a typo for an
archival document: it must preserve segment IDs and timestamps exactly (returns `{id, corrected_text}`),
IDs are validated as an exact set (missing, extra or duplicate IDs → the window is retried, then falls
back to the raw text), and a length guard rejects corrections that grow or shrink a segment by more
than 2×/0.5×. `raw_text` is never modified, and every change is written to a diff log.

## 2. Why sampled frames, not native video input

- **Control over cost.** A frame is a bounded, countable unit: frame cap, scene sensitivity,
  coverage interval, downscale and image detail are all direct levers, and the estimate step can
  project spend before any API call. Native video input prices and samples internally.
- **Portability.** Image input over Chat Completions is the lowest common denominator across
  OpenAI-compatible servers (vLLM, RunPod). Native video isn't, and the brief requires the vision and
  text stages to be swappable by config.
- **Timestamps you can trust.** Every frame carries an exact timestamp in its label, so scene
  descriptions map to real moments, and a reader can open *the very frame the model saw*.
- **Inspectable and cacheable.** Frames are files; vision batches are cached per batch; a failed batch
  is a visible gap rather than an opaque failure.
- **Right sampling for this content.** Talks, demos and screen recordings are mostly static between
  events. Scene-change detection finds the events, and still-screen detection (ffmpeg
  `freezedetect`) finds the slide or screen changes that only change text, which the scene score
  misses; a coverage interval covers static stretches and the end of the video; perceptual-hash
  dedupe removes repeats, confirmed by a pixel difference on screen-like frames so a changed line of
  text is not taken for a repeat. The audio track carries continuous information, so nothing is
  lost by not sending every frame. (Fast-motion footage without narration is where this approach is
  weakest; raise the frame cap and sensitivity.)

## 3. Timestamps vs transcription model choice

Timestamps are the backbone of the output format (chapter ranges, quotes, anchors),
so the transcription model's timestamp ability matters more than its raw accuracy:

| Model family | Segment timestamps | Consequence |
|---|---|---|
| `whisper-1` (`verbose_json`) | yes | long chunks (~10 min), real per-segment times |
| `gpt-4o-*-transcribe` | no (`json` only) | must use short chunks; chunk boundaries become the timestamps |
| `gpt-transcribe` (current) | **undocumented** | try segments, degrade gracefully |

`whisper-1` is deprecated (shutdown 2027-02-26), so a design that hard-wired it would break in a few
months. Instead the transcriber advertises **capabilities**, and the pipeline plans from them:

- **Segment mode:** ≤10 min chunks with ~1 s overlap. Overlap is deduped by a *plan-based* rule: the cut
  point between two chunks is the middle of their overlap, and each chunk keeps segments whose midpoint
  falls on its side. The result for a prefix of chunks equals a prefix of the full result, which is what
  lets transcript segments be emitted as chunks finish. Words repeated across a boundary are
  trimmed.
- **Chunk mode:** 30-60 s chunks (default 45 s), no overlap, one segment per chunk with the chunk's
  boundaries as its timestamps. The document records `timestamp_precision: chunk` and the appendix says
  timestamps are approximate. The tradeoff: ±chunk-length precision on quotes and chapter boundaries
  (acceptable for navigation; weaker for subtitle-grade use) in exchange for working with any model.
- **Unknown capability is probed, not guessed.** If a model rejects `verbose_json`, the transcriber
  flips its capability (remembered in `<home>/capabilities.json`), the runner re-plans audio and
  transcription with short chunks, and the job continues. The doctor's deep check can do this probe
  up front with a one-second silent clip.
- Chapter boundaries are validated against the *video duration* regardless of timestamp precision, and
  every timestamp in the Markdown is clamped to it.

## 4. Caching, resume and re-runs

Goal: never pay twice for the same work, and survive restarts.

- **Stage key** = `sha256(video hash, stage, stage version, the settings that stage depends on, the keys
  of the stages it depends on)`. Change the glossary and transcription, vision, correction and
  synthesis re-run; change only the synthesis model and only synthesis (and assemble) re-run; change
  the frame cap and frames, vision and everything after it re-run.
- **Keys chain on content, not just on configuration.** Each stage's cache also stores a hash of its
  *output*, and downstream keys include it. So if a stage is re-executed (e.g. forced) and its output
  changes, everything after it is invalidated; if it reproduces identical output, downstream caches
  stay valid.
- **Unit caches** inside the expensive stages: one file per audio chunk, vision batch, correction
  window, chapter summary. A crash or cancel mid-stage loses at most the unit in flight; a retry of a
  failed vision stage re-asks only the batches that failed.
- **Resume:** job state and stage results are JSON files written atomically (temp file + rename).
  Running a job again, in the same process or a new one, continues from the first stage whose cache
  is missing. Cancelling marks the job `cancelled` and loses nothing that was cached.
- **Retry is just running again.** `force` invalidates specific stages, and through the chained keys
  whatever depends on their output.

## 5. Failure policy

- **Fatal vs soft.** Errors that retrying or continuing cannot fix (bad key, no quota, model not found,
  permission denied) fail the stage immediately with a human-readable message (and never include the
  key). Transient errors (429, 5xx, timeouts) are retried with exponential backoff and jitter.
- **Fail-soft stages.** A vision batch that still fails becomes a recorded gap (warning, Processing
  Notes entry), and the pipeline continues, *unless every batch fails*, which indicates a
  systemic problem. A failed correction window falls back to raw text; a failed chapter summary gets a
  placeholder and a warning. A failed stage never discards earlier results: they stay cached.
- **Chapter validation.** A proposal must be contiguous, start at 0, end at the duration and use real
  timestamps. Minor defects (gaps/overlaps) are repaired deterministically (chapter start times define
  the partition); major ones (timestamps beyond the video) are retried with feedback; as a last resort
  an even split is used. Whatever happens, the stored chapters are valid.
- **Quotes must be verbatim.** Each quote is checked against the transcript (case/punctuation
  tolerant, can span segments), its timestamp is snapped to the segment that contains it, and anything
  not found is dropped with a warning.

## 6. Deterministic output

An LLM never decides structure. Frontmatter, headings, anchors, the table of contents, transcript
lines, the entity index (computed from entity lists and a text search of the transcript), the
appendix, and every timestamp format come from code. Model-written text is inserted as inline text with
heading/rule syntax neutralized, so a model cannot inject a heading and break the table of contents or
anchor scheme. Assembly is a pure function of the analysis object, which is why it has a golden-file
test.

## 7. Interfaces and swappability

`Transcriber`, `VisionAnalyzer` and `TextLLM` are protocols in `providers/base.py`. The OpenAI
implementation uses Chat Completions (not the Responses API) because that is what OpenAI-compatible
servers implement. Base URLs and keys are per role. The test suite and the `fake` profile
run on deterministic fakes. The prompts contain machine-readable markers (`TARGET SEGMENTS`, `WINDOW_RANGE`,
`CHAPTER_RANGE`) so fakes can respond meaningfully without any real model.

## 8. Events

Stages report progress through `PipelineContext.emit` (stage state, log lines, transcript chunks,
frames, scenes, chapters, synthesis). The engine (`engine.py`) passes them to an optional callback,
which the CLI (milestone M1) will use for progress on stderr. Faircopy streamed the same events to its web UI over SSE;
that layer was dropped with the web stack.

## 9. Known limits and deliberate tradeoffs

- **Estimate accuracy:** token counts use configurable heuristics (speech ≈220 tokens/min; per-image
  constants by detail level) and are labelled as assumptions; actual usage is recorded per stage.
- **Diarization** is wired but experimental: its OpenAI model is deprecated with no named replacement.
- **Live OpenAI behaviour was not exercised in development** (no key was available while building).
  Everything that touches the API is covered by a mocked-transport test suite, and the capability
  probing/fallback paths exist to absorb differences; the doctor's deep check is the first thing to
  run with a real key (PLAN milestone M5).
