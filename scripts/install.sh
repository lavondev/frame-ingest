#!/bin/sh
# One-step install from a checkout of the repository:
#
#   git clone https://github.com/lavondev/frame-ingest ~/.frame-ingest-src
#   ~/.frame-ingest-src/scripts/install.sh
#
# It (1) builds the tool's environment now, so the first /frame-ingest is fast, and (2) links
# the skill into Claude Code (~/.claude/skills) and the shared agent folder that Codex and others
# read (~/.agents/skills). Nothing is copied outside those links; re-running is safe.
# Set FRAME_INGEST_SKIP_WARM=1 to skip step 1. Needs uv: https://docs.astral.sh/uv/
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd -P)
skill="$repo/skills/frame-ingest"
[ -f "$skill/SKILL.md" ] || { echo "error: $skill/SKILL.md not found" >&2; exit 1; }

if [ "${FRAME_INGEST_SKIP_WARM:-}" != "1" ]; then
  command -v uv >/dev/null 2>&1 || {
    echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 4
  }
  echo "Building the environment (first time takes a minute or two)..."
  uv sync --quiet --project "$repo" --extra local --extra url
fi

link() {
  dir="$1"
  target="$dir/frame-ingest"
  mkdir -p "$dir"
  if [ -L "$target" ]; then
    rm "$target"
  elif [ -e "$target" ]; then
    echo "skipped $target: something that is not a link is already there" >&2
    return 0
  fi
  ln -s "$skill" "$target"
  echo "linked $target"
}

link "${HOME}/.claude/skills"
link "${HOME}/.agents/skills"

cat <<'MSG'

Done. Start a new Claude Code session and type:   /frame-ingest <video file or URL>
In Codex (new session) type:                      $frame-ingest <video file or URL>
MSG
