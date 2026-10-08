# A finished template set

A 48-second screen recording of a database migration, with a local transcript. These are the three
files after the agent filled them; `fi check` says `ok` for each. Your frame names, segment ids
and chapter times come from your own templates: never copy these.

## `vision/batch-01.json`

```json
{
  "sheet": "/Users/you/.frame-ingest/jobs/1f2e3d4c5b6a/agent/sheets/sheet-01.jpg",
  "frames": [
    {
      "frame": "frame_0000.00.jpg",
      "time": "00:00:00",
      "scene_description": "Title slide reading 'Zero-downtime Postgres migrations'.",
      "on_screen_text": ["Zero-downtime Postgres migrations", "orders table · 40M rows"],
      "change_from_previous": "First frame",
      "entities": [{"name": "Postgres", "kind": "technology"}],
      "scene_type": "title_card"
    },
    {
      "frame": "frame_0012.50.jpg",
      "time": "00:00:12",
      "scene_description": "Terminal running a row count on the orders table.",
      "on_screen_text": ["SELECT count(*) FROM orders;", "40,112,887"],
      "change_from_previous": "The slide gives way to a terminal.",
      "entities": [],
      "scene_type": "screen_recording"
    },
    {
      "frame": "frame_0031.00.jpg",
      "time": "00:00:31",
      "scene_description": "Editor showing a CREATE INDEX CONCURRENTLY statement.",
      "on_screen_text": ["CREATE INDEX CONCURRENTLY idx_status_v2 ON orders (status_v2);"],
      "change_from_previous": "The terminal is replaced by a code editor.",
      "entities": [{"name": "CREATE INDEX CONCURRENTLY", "kind": "technology"}],
      "scene_type": "screen_recording"
    }
  ]
}
```

## `corrections.json`

The `todo` line is gone; only misheard words changed, every id is kept.

```json
{
  "segments": [
    {"id": 0, "corrected_text": "Today we're doing a zero-downtime migration on Postgres."},
    {"id": 1, "corrected_text": "The orders table has about forty million rows, so we can't lock it."},
    {"id": 2, "corrected_text": "Use CREATE INDEX CONCURRENTLY instead."}
  ]
}
```

## `synthesis.json`

```json
{
  "title": "Zero-downtime Postgres migration",
  "tldr": "Change a 40-million-row table without locking it, building the index with CREATE INDEX CONCURRENTLY.",
  "abstract": "A short screen recording that sizes up a large orders table and shows why its new index must be built concurrently.",
  "glossary": [
    {"term": "CREATE INDEX CONCURRENTLY", "definition": "Builds an index without blocking writes.", "first_seen_s": 31.0}
  ],
  "open_questions": ["How long did the concurrent index build take?"],
  "tags": ["postgres", "migrations", "indexes"],
  "chapters": [
    {
      "title": "The problem: 40M rows, no locks",
      "start": 0,
      "end": 24.0,
      "summary": "The speaker introduces the migration and the size of the orders table, which rules out locking it.",
      "key_points": ["The orders table has about 40 million rows", "The table cannot be locked"],
      "quotes": [{"t": 12.5, "text": "The orders table has about forty million rows, so we can't lock it."}],
      "entities": [{"name": "Postgres", "kind": "technology"}],
      "decisions_claims": ["Locking the table is not acceptable."],
      "visual_summary": "A title slide, then a terminal with a row count."
    },
    {
      "title": "Building the index concurrently",
      "start": 24.0,
      "end": 48.0,
      "summary": "The fix is to build the new index with CREATE INDEX CONCURRENTLY.",
      "key_points": ["Use CREATE INDEX CONCURRENTLY for large tables"],
      "quotes": [{"t": 31.0, "text": "Use CREATE INDEX CONCURRENTLY instead."}],
      "entities": [],
      "decisions_claims": [],
      "visual_summary": "A code editor with the CREATE INDEX CONCURRENTLY statement."
    }
  ]
}
```
