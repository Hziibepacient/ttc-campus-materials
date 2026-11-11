#!/usr/bin/env bash
# EventLens installer for macOS / Linux. Safe to run again.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  echo "[1/5] Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"
else echo "[1/5] uv already installed"; fi
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  if command -v brew >/dev/null; then echo "[2/5] Installing FFmpeg..."; brew install ffmpeg
  else echo "Please install FFmpeg (e.g. sudo apt install ffmpeg) and run again."; exit 1; fi
else echo "[2/5] FFmpeg already installed"; fi
wheel="$(ls "$here"/eventlens-*.whl | head -1)"
echo "[3/5] Installing EventLens from $(basename "$wheel")..."
uv tool install --python 3.12 --force "$wheel"
echo "[4/5] Downloading the detection model (YOLOX-S, ~36 MB, sha256 verified)..."
eventlens models fetch
echo "[5/5] Installing the AI-assistant skill for Codex and Claude Code..."
eventlens skill install --force
echo; eventlens doctor || true
echo; echo "Done. Restart Codex / Claude Code so it picks up the skill."
