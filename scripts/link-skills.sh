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
for agent in task-builder task-lister task-commenter; do
  AGENT_DEST="$HOME/.claude/agents/$agent.md"
  if [[ -e "$AGENT_DEST" && ! -L "$AGENT_DEST" ]]; then
    echo "error: $AGENT_DEST exists and is not a symlink — remove the old copy first" >&2
    exit 1
  fi
  ln -sfn "$REPO_ROOT/.claude/agents/$agent.md" "$AGENT_DEST"
  echo "linked $AGENT_DEST -> $(readlink "$AGENT_DEST")"
done

# task_ref.py onto PATH as `task-ref`. The skills and the task-lister agent
# invoke it by bare name so nothing hardcodes this repo's location — move the
# checkout and a re-run of this script is the only fixup needed.
BIN_DEST="$HOME/.local/bin"
mkdir -p "$BIN_DEST"
if [[ -e "$BIN_DEST/task-ref" && ! -L "$BIN_DEST/task-ref" ]]; then
  echo "error: $BIN_DEST/task-ref exists and is not a symlink — remove the old copy first" >&2
  exit 1
fi
ln -sfn "$REPO_ROOT/scripts/task_ref.py" "$BIN_DEST/task-ref"
echo "linked $BIN_DEST/task-ref -> $(readlink "$BIN_DEST/task-ref")"
case ":$PATH:" in
  *":$BIN_DEST:"*) ;;
  *) echo "warning: $BIN_DEST is not on PATH — task listings will fail to find task-ref" >&2 ;;
esac
