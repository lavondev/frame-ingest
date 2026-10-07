"""URL ingest (docs/PLAN.md M3): URL policy, a pinned-IP HTTP fetcher for direct media links, and
a hardened yt-dlp wrapper for everything else. ffmpeg never sees a URL: whatever is fetched lands
as a regular file in a private directory and is then adopted into a job like any local file."""
