#!/usr/bin/env bash
# Prepare everything the MCM-Net predictor needs, next to this repository:
#   ../MCM_streaming                 model + feature code (cloned read-only, never modified)
#   ../hf_artifacts/weights/combined deployed model from the private Hugging Face repo (needs HF_TOKEN)
#   ../hf_artifacts/envtiles         MCM's deployment environment tiles (Finland + Norway coast, from the same repo)
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
TILES="$WORK/hf_artifacts/envtiles"
MCM_TILES="$TILES/tiles_finland_full,$TILES/tiles_norway_full"
HF="https://huggingface.co/mark000071/MCM_streaming/resolve/main"

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
fetch() {  # fetch <repo path> <local file>, skipped when already there
  [ -s "$2" ] && return 0
  : "${HF_TOKEN:?set HF_TOKEN to a Hugging Face token with read access to mark000071/MCM_streaming}"
  mkdir -p "$(dirname "$2")"
  curl -fsSL -H "Authorization: Bearer $HF_TOKEN" -o "$2.part" "$HF/$1" && mv "$2.part" "$2"
}
# model + memory bank; scorer_online_final = the deployed served-rule scorer; type_cache = MMSI -> ship class
for f in model_ae.pt frozen_feature_stats.json preprocessing_config.json \
         memory_bank/filter_past.pt memory_bank/filter_fut.pt \
         scorer_online_final.npz scorer_online_final.json type_cache.json; do
  fetch "weights/combined/$f" "$MCM_WEIGHTS/$f"
done

echo "== 3/4 torch ($DEVICE) + scipy into the uv environment"
uv sync -q
if [ "$DEVICE" = "cuda" ]; then
  uv pip install -q torch --index-url https://download.pytorch.org/whl/cu124
else
  uv pip install -q torch --index-url https://download.pytorch.org/whl/cpu
fi
uv pip install -q scipy
# note: a later plain `uv sync` removes these again; `uv run` keeps them

echo "== 4/4 environment tiles -> $TILES"
for region in finland norway; do
  if [ ! -f "$TILES/tiles_${region}_full/coverage.json" ]; then
    fetch "envtiles/tiles_${region}_full.tar.gz" "$TILES/tiles_${region}_full.tar.gz"
    tar xzf "$TILES/tiles_${region}_full.tar.gz" -C "$TILES"
  fi
done

echo
echo "done. Enable MCM-Net for this shell with:"
echo "  eval \"\$(scripts/setup_mcmnet.sh --print-env)\""
