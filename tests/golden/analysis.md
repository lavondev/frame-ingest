---
title: Widget Frobnicator Walkthrough
source_file: demo video.mp4
duration: 00:01:30
duration_seconds: 90.0
resolution: 320x240
analyzed_at: '2026-10-04T12:30:05Z'
models:
  transcribe: whisper-1
  vision: v-model
  correct: c-model
  synthesize: s-model
has_audio: true
chapter_count: 2
tags:
- tutorial
- setup
---

# Widget Frobnicator Walkthrough

## TL;DR {#tldr}

A short setup and tuning tutorial.

## Abstract {#abstract}

Covers setup then cache tuning.

## Table of Contents {#toc}

- [Chapter 1: Setup [00:00:00 - 00:00:45]](#ch-01)
- [Chapter 2: Tuning [00:00:45 - 00:01:30]](#ch-02)
- [Glossary](#glossary)
- [Entity Index](#entity-index)
- [Open Questions](#open-questions)
- [Appendix: Processing Notes](#appendix)

## Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01}

*Source: demo video.mp4 · Chapter 1 of 2*

### Summary — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-summary}

How to install the product.
\# not a heading

**Entities:** Widget Frobnicator

### Key Points — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-key-points}

- Open dashboard
- Pick settings

**Decisions and claims:**

- Settings are stored locally.

### Visual Description — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-visual}

A dashboard is shown.

- [00:00:01](#t-000001) *(slide)* Dashboard home screen. — `frame_0001.00.jpg`

### On-Screen Text — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-on-screen-text}

- Widget Frobnicator
- Step 1

### Notable Quotes — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-quotes}

> "Open the dashboard first." — [00:00:06](#t-000006)

### Corrected Transcript — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-transcript}

<a id="t-000001"></a>**[00:00:01]** Welcome to the Widget Frobnicator.
<a id="t-000006"></a>**[00:00:06]** Open the dashboard first.

## Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02}

*Source: demo video.mp4 · Chapter 2 of 2*

### Summary — Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02-summary}

Tune the cache.

**Entities:** Cache

### Key Points — Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02-key-points}

- Cache size matters

### Visual Description — Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02-visual}

- [00:00:50](#t-000050) *(screen_recording)* Cache settings panel. — `frame_0050.00.jpg`

### On-Screen Text — Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02-on-screen-text}

_No on-screen text detected._

### Notable Quotes — Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02-quotes}

_None._

### Corrected Transcript — Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02-transcript}

<a id="t-000050"></a>**[00:00:50]** Now tune the cache size.
<a id="t-000050-2"></a>**[00:00:50]** It matters a lot.

## Glossary {#glossary}

- **Cache** — Fast temporary storage. (first seen [00:00:50](#t-000050))

## Entity Index {#entity-index}

- **Widget Frobnicator** (product) — 3 mention(s) — [Ch 01](#ch-01) — [00:00:00](#ch-01), [00:00:01](#t-000001)
- **Cache** (concept) — 2 mention(s) — [Ch 02](#ch-02) — [00:00:45](#ch-02), [00:00:50](#t-000050)

## Open Questions {#open-questions}

- What cache size for huge workloads?

## Appendix: Processing Notes {#appendix}

- **Source:** demo video.mp4 (00:01:30, 320x240, sha256 `abababababab`)
- **Analyzed at:** 2026-10-04 12:30:05 UTC
- **Stages run:** probe, transcribe, vision
- **Stages reused from cache:** frames
- **Models:** transcribe=whisper-1, vision=v-model, correct=c-model, synthesize=s-model
- **Frames analysed:** 2 of 2 selected
- **Transcript:** 4 segment(s), 1 audio chunk(s), timestamp precision: segment
- **Segments changed by correction:** 1
- **Token usage:**
  - vision / v-model: 1 call(s), 300 input, 120 output tokens
- **Warnings (1):**
  - [vision] Vision analysis failed for 1 frame(s).
- **Failed batches (1):**
  - vision batch 3 (00:01:10 - 00:01:20): timeout
