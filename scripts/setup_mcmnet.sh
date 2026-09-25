#!/usr/bin/env bash
# Prepare everything the MCM-Net predictor needs, next to this repository:
#   ../MCM_streaming                 model + feature code (cloned read-only, never modified)
#   ../hf_artifacts/weights/combined deployed model from the private Hugging Face repo (needs HF_TOKEN)
#   ../envtiles/finland              OSM environment tiles for the Finnish coast
# and install torch + scipy into the uv environment.
#
#   HF_TOKEN=hf_... scripts/setup_mcmnet.sh [cpu|cuda]      (default: cuda if nvidia-smi works, else cpu)
#
# Then: eval "$(scripts/setup_mcmnet.sh --print-env)" and run `uv run envship dev`.
set -euo pipefail
cd "$(dirname "$0")/.."
WORK="$(cd .. && pwd)"
MCM_ROOT="$WORK/MCM_streaming"
MCM_WEIGHTS="$WORK/hf_artifacts/weights/combined"
MCM_TILES="$WORK/envtiles/finland"

if [ "${1:-}" = "--print-env" ]; then
  device=cpu
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then device=cuda; fi
  echo "export MCM_ROOT=$MCM_ROOT MCM_WEIGHTS=$MCM_WEIGHTS MCM_TILES=$MCM_TILES MCM_DEVICE=$device"
  exit 0
fi

DEVICE="${1:-}"
if [ -z "$DEVICE" ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then DEVICE=cuda; else DEVICE=cpu; fi
fi

echo "== 1/4 MCM_streaming code -> $MCM_ROOT"
[ -d "$MCM_ROOT/.git" ] || git clone -q https://github.com/mark000071/MCM_streaming.git "$MCM_ROOT"

echo "== 2/4 weights/combined -> $MCM_WEIGHTS"
if [ ! -s "$MCM_WEIGHTS/model_ae.pt" ]; then
  : "${HF_TOKEN:?set HF_TOKEN to a Hugging Face token with read access to mark000071/MCM_streaming}"
  mkdir -p "$MCM_WEIGHTS/memory_bank"
  for f in model_ae.pt frozen_feature_stats.json preprocessing_config.json scorer_head.pkl \
           memory_bank/filter_past.pt memory_bank/filter_fut.pt memory_bank/part_traj.pt; do
    curl -fsSL -H "Authorization: Bearer $HF_TOKEN" -o "$MCM_WEIGHTS/$f" \
      "https://huggingface.co/mark000071/MCM_streaming/resolve/main/weights/combined/$f"
  done
fi

echo "== 3/4 torch ($DEVICE) + scipy + osmium into the uv environment"
uv sync -q
if [ "$DEVICE" = "cuda" ]; then
  uv pip install -q torch --index-url https://download.pytorch.org/whl/cu124
else
  uv pip install -q torch --index-url https://download.pytorch.org/whl/cpu
fi
uv pip install -q scipy osmium
# note: a later plain `uv sync` removes these again; `uv run` keeps them

echo "== 4/4 OSM environment tiles -> $MCM_TILES"
if [ ! -f "$MCM_TILES/coverage.json" ] || ! grep -q '"completed_bboxes"' "$MCM_TILES/coverage.json"; then
  mkdir -p "$WORK/osm" "$MCM_TILES"
  PBF="$WORK/osm/finland-latest.osm.pbf"
  [ -s "$PBF" ] || curl -fsSL -o "$PBF" https://download.openstreetmap.fr/extracts/europe/finland-latest.osm.pbf
  # the fixture spans 57.8-65.8 N, 17.0-30.9 E; resumable per 1-degree latitude slab
  (cd "$MCM_ROOT/serving" && TMPDIR="$WORK/envtiles" uv run --project "$OLDPWD" python -m aisstream.envtiles.build_tiles \
    --pbf "$PBF" --out "$MCM_TILES" --bbox 57.5,17.0,66.0,31.0 --tile-deg 0.25 --slab-deg 1.0)
fi

echo
echo "done. Enable MCM-Net for this shell with:"
echo "  eval \"\$(scripts/setup_mcmnet.sh --print-env)\""
