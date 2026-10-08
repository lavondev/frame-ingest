frame-ingest · Faircopy · how the two actually work

# Frame-Ingest Flow Lab

Claude and Codex can't watch a video. Both tools cut it into two things a model can read: **frames** (still images) and a **transcript** (the audio as text). The difference is who does the looking, and whether the audio reliably makes it in.

**A****Faircopy** calls cloud AI itself: speech-to-text plus a vision model. Needs API keys.

**B****frame-ingest** hands the frames to the agent you're already running, so no key. Audio depends on a local speech model.

Side by side

## Who does what

YouAgentfi CLILocal modelCloud APIFaircopy app

A · the app

### Faircopy

1. YouUpload in the web app
2. AppSplit audio, pick frames
3. CloudTranscribe the audio
4. CloudDescribe each frame
5. CloudFix transcript, write chapters
6. AppBuild the document

Hears and sees everything. Costs API money, needs keys, and only runs inside the app.

B · today

### frame-ingest (agent mode)

1. You`/frame-ingest video.mp4`
2. CLIPick frames, make contact sheets
3. LocalTranscribe *if* Whisper is installed
4. AgentLooks at sheets, writes JSON
5. CLIValidates, rejects, agent retries
6. CLIBuilds the document

No key, works in any harness. But audio can silently drop out, and the agent guesses file formats.

C · proposed

### frame-ingest, hardened

1. You`/frame-ingest video.mp4`
2. CLI`fi ingest`: one call does everything mechanical
3. LocalAudio is required, with a retry and a loud fail
4. CLIWrites fill-in-the-blank templates
5. AgentFills them in, `fi check` each one
6. CLI`fi finish`: build, validate, scan

Faircopy's understanding with no key. If a cloud key is set and you say yes, it switches to Faircopy-style pipeline mode.

Interactive demo

## Run each version on the same video

The sample video is a 93-second screen recording, *“Zero-downtime Postgres migration”*. Pick a version, press **Run** (or **Step**), and watch what each actor does and what you get at the end. Click any step to expand it.

Mock data · no real calls

Speed

Proposal

## A bulletproof architecture

The shape stays the same: a thin skill on top of a trusted CLI. What changes is that the agent never has to guess. Every mechanical step becomes one command, every file it writes starts as a template, and every write is checked on the spot. This follows Anthropic's and OpenAI's current skill guidance: keep the skill short, put fragile steps in scripts, give templates, and validate before you execute.

### 1 · Skill

What the agent reads. Same folder for Claude Code and Codex.

**SKILL.md ≤ 150 lines**Triggers in the description, 4 commands, the rules. Nothing else.

**references/**One level deep: schemas, examples, troubleshooting. Read only when needed.

**agents/openai.yaml**Codex display name, icon, implicit-invocation policy.

**plugin.json**Claude Code plugin and marketplace install.

**evals/**Golden videos plus expected behavior, run on Haiku, Sonnet, Opus and Codex.

### 2 · Launcher

`scripts/fi` finds or installs the CLI.

**repo → PATH → pinned uvx**Same three routes as today.

**prints its own path**`fi ingest` returns `fi_path`, and the agent reuses that exact string. This ends the `${CLAUDE_SKILL_DIR}` exit-127 bug.

**speech extra by default**The launcher always installs `[local]`. Audio is not optional.

### 3 · CLI state machine

The agent always knows its next move.

fi ingest \<video|url>→ agent fills templates→ fi check \<file>⟲ until ok → fi finish \<job>→ document + coverage line

**fi ingest**doctor + estimate + probe + transcribe + frames + sheets + templates, in one call. Returns a task card: what to read, which files to fill, and the next command.

**fi next \<job>**Resumable. If the agent loses track or the context resets, it says exactly what's left.

**fi check \<file>**Validates one file in milliseconds and names the line and rule. Small loops instead of one big failure.

**fi finish**assemble + validate + scan, so it can't skip a step.

### 4 · Engine

The Faircopy pipeline, ported. Pick who does the thinking.

probe→audio→transcribe→frames→vision→correct→synthesize→assemble

**host-agent provider**The agent you're running does vision, correct and synthesize. No key. This is the default.

**local provider**Ollama or any OpenAI-compatible local server. Nothing leaves the machine.

**cloud provider**Faircopy behavior. Only with a key *and* your explicit yes.

**audio guarantee**If audio exists and 0 words come back, retry without the voice filter, then fail loudly. The document header always shows `audio: yes/no`.

**subagent fan-out**Long videos: vision batches go to parallel subagents, so the main context stays small.

### 5 · Guards

Unchanged from the plan (T1 to T11).

**video text = data**Sanitized, scanned and never obeyed.

**safe fetch**SSRF block, pinned yt-dlp floor, ffmpeg file-only.

**job jail**Writes only inside the job dir; symlinks refused.

**egress consent**Nothing goes to the cloud without a yes. `--offline` hard-blocks.

### Which mode runs

| Situation | Mode | Audio | Who looks at frames | Cost |
| --- | --- | --- | --- | --- |
| Default, no key | Agent mode | Local Whisper | Your Claude or Codex session | Your plan's usage |
| Ollama running | `--profile local` | Local Whisper | Local vision model | Free, slower |
| Key set + you say yes | `--profile cloud` | Cloud speech API | Cloud vision model | API cents per video |
| Video over \~20 min | Agent mode + subagents, or ask to switch | Local Whisper | Parallel subagents | Shown before running |

### The rules it's built on

### Short skill, long references

Only the name and description load up front. SKILL.md loads when triggered; references load on demand and stay one level deep.

Anthropic best practices · OpenAI Codex skills

### Low freedom for fragile steps

Probing, transcribing, frame picking and assembly are scripts, not prose. The agent only does the judgment work: seeing and summarizing.

Anthropic: degrees of freedom

### Plan → validate → execute

The agent writes structured JSON, a script validates it, and only then does the CLI build. `fi check` makes that loop small.

Anthropic: feedback loops

### Templates over descriptions

Prefilled files with every frame name and segment id. The agent can't invent the wrong shape, which removes your 9-problem failure.

Anthropic: template pattern

### Description does the triggering

Front-load trigger words (video, recording, lecture, URL), state what and when, and keep it under 1,024 characters.

Both vendors

### Don't assume tools exist

`fi ingest` checks ffmpeg, uv and Whisper, and returns the exact fix. It never fails silently.

Anthropic: dependencies

### Evals before docs

Three golden videos (speech, music under speech, silent) with expected outcomes, run in CI against several models and both harnesses.

Anthropic: evaluation-driven

### Treat the caller as untrusted

The CLI assumes its arguments might come from a hijacked agent. No flag runs code, writes outside the job or reveals keys.

frame-ingest AGENTS.md

Sources

- [Skill authoring best practices, Claude Docs](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- [Equipping agents for the real world with Agent Skills, Anthropic](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- [Agent Skills, OpenAI Codex docs](https://developers.openai.com/codex/skills)
- [Writing effective SKILL.md files for Codex CLI](https://codex.danielvaughan.com/2026/03/26/writing-effective-skillmd-files/)
- frame-ingest source: `agent/prepare.py`, `pipeline/*.py`, `skills/frame-ingest/SKILL.md`