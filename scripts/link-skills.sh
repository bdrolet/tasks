#!/usr/bin/env bash
# Symlink the Asana consumer skills into ~/.claude/skills/ and the
# task-builder agent into ~/.claude/agents/ (per-skill symlinks —
# NEVER symlink the parent directory: a v2.1.69 Claude Code security fix
# skips user-level skills entirely when ~/.claude/skills itself is a
# symlink). Mirrors ~/src/docs/scripts/link-skills.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.claude/skills"
mkdir -p "$DEST"

for skill in searching-tasks fetching-task editing-tasks creating-tasks planning-project-tasks; do
  # ln -sfn onto a real (non-symlink) directory fails and set -e aborts the
  # loop half-linked; guard so a pre-existing real copy fails loudly instead.
  if [[ -e "$DEST/$skill" && ! -L "$DEST/$skill" ]]; then
    echo "error: $DEST/$skill exists and is not a symlink — remove the old copy first" >&2
    exit 1
  fi
  ln -sfn "$REPO_ROOT/.claude/skills/$skill" "$DEST/$skill"
  echo "linked $DEST/$skill -> $(readlink "$DEST/$skill")"
done

mkdir -p "$HOME/.claude/agents"
AGENT_DEST="$HOME/.claude/agents/task-builder.md"
if [[ -e "$AGENT_DEST" && ! -L "$AGENT_DEST" ]]; then
  echo "error: $AGENT_DEST exists and is not a symlink — remove the old copy first" >&2
  exit 1
fi
ln -sfn "$REPO_ROOT/.claude/agents/task-builder.md" "$AGENT_DEST"
echo "linked $AGENT_DEST -> $(readlink "$AGENT_DEST")"
