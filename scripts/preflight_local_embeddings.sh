#!/usr/bin/env bash
# Preflight check for the local BGE-M3 embedding model.
#
# Validates that the host can run sentence-transformers with BAAI/bge-m3,
# downloads the model into the HuggingFace cache, runs a smoke test, and
# benchmarks throughput against a 3000-SKU synthetic catalog so you know
# how long the real sync will take BEFORE wiring it into the project.
#
# Idempotent: re-running uses the cached model + venv (no re-downloads).
#
# Usage:
#   bash scripts/preflight_local_embeddings.sh
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VENV_DIR="${HOME}/tmp/bge-preflight/.venv"
WORK_DIR="${HOME}/tmp/bge-preflight"
MODEL_NAME="BAAI/bge-m3"
MIN_DISK_MB=2048   # 2GB
MIN_RAM_MB=2048    # 2GB
SKU_COUNT="${SKU_COUNT:-3000}"
BATCH_SIZE="${BATCH_SIZE:-32}"

# ANSI colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

ok()    { echo -e "${GREEN}✓${RESET} $1"; }
warn()  { echo -e "${YELLOW}!${RESET} $1"; }
fail()  { echo -e "${RED}✗${RESET} $1"; exit 1; }
info()  { echo -e "${BLUE}→${RESET} $1"; }
title() { echo -e "\n${BOLD}$1${RESET}"; }

# ---------------------------------------------------------------------------
# Step 1 — Preflight checks
# ---------------------------------------------------------------------------

title "Step 1/4 — Preflight checks"

# Python version
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found. Install Python 3.12+ first."
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
    fail "Python $PY_VERSION found, need 3.10+"
fi
ok "Python $PY_VERSION"

# uv
if ! command -v uv >/dev/null 2>&1; then
    fail "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
ok "uv $(uv --version | awk '{print $2}')"

# Disk space (in HOME)
DISK_AVAIL_MB=$(df -m "$HOME" | awk 'NR==2 {print $4}')
if [[ "$DISK_AVAIL_MB" -lt "$MIN_DISK_MB" ]]; then
    fail "Only ${DISK_AVAIL_MB}MB free in \$HOME, need ${MIN_DISK_MB}MB"
fi
ok "Disk space: ${DISK_AVAIL_MB}MB available"

# RAM
RAM_AVAIL_MB=$(free -m | awk 'NR==2 {print $7}')
if [[ "$RAM_AVAIL_MB" -lt "$MIN_RAM_MB" ]]; then
    warn "Only ${RAM_AVAIL_MB}MB free RAM, recommended ${MIN_RAM_MB}MB+"
else
    ok "RAM available: ${RAM_AVAIL_MB}MB"
fi

# GPU detection
GPU_TYPE="cpu"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
    ok "NVIDIA GPU: $GPU_NAME"
    GPU_TYPE="cuda"
elif [[ "$(uname)" == "Darwin" ]] && [[ "$(uname -m)" == "arm64" ]]; then
    ok "Apple Silicon detected (will use MPS)"
    GPU_TYPE="mps"
else
    warn "No GPU detected — will use CPU (slower but functional)"
fi

# ---------------------------------------------------------------------------
# Step 2 — Set up isolated venv
# ---------------------------------------------------------------------------

title "Step 2/4 — Setting up isolated venv"

mkdir -p "$WORK_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating venv at $VENV_DIR"
    uv venv "$VENV_DIR"
else
    ok "Reusing existing venv at $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Install sentence-transformers if missing
if ! python -c "import sentence_transformers" 2>/dev/null; then
    info "Installing sentence-transformers (this downloads torch CPU ~500MB)..."
    uv pip install --quiet sentence-transformers
    ok "sentence-transformers installed"
else
    ok "sentence-transformers already installed"
fi

# ---------------------------------------------------------------------------
# Step 3 — Download model + smoke test
# ---------------------------------------------------------------------------

title "Step 3/4 — Loading $MODEL_NAME (first run downloads ~570MB)"

python <<PYEOF
import time
from sentence_transformers import SentenceTransformer

t0 = time.time()
model = SentenceTransformer("$MODEL_NAME")
print(f"  Model loaded in {time.time() - t0:.1f}s")

# Smoke test
t0 = time.time()
vectors = model.encode([
    "Quilmes Cristal 1L | marca: QUILMES | tipo: CERVEZAS | formato: RETORNABLE 1000",
    "Brahma Lata 473 | marca: BRAHMA | tipo: CERVEZAS | formato: LATA 473",
    "dame dos cajones de la rubia",  # query coloquial
])
print(f"  Smoke test: encoded 3 texts in {(time.time() - t0)*1000:.0f}ms")
print(f"  Output shape: {vectors.shape}")
print(f"  Native dimensions: {vectors.shape[1]}")
print(f"  Truncated to 512 (Matryoshka) sample: {vectors[0][:512].shape}")
PYEOF

ok "Model is functional"

# ---------------------------------------------------------------------------
# Step 4 — Benchmark with synthetic catalog
# ---------------------------------------------------------------------------

title "Step 4/4 — Benchmarking $SKU_COUNT synthetic SKUs (batch_size=$BATCH_SIZE)"

python <<PYEOF
import time
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("$MODEL_NAME")

# Synthetic catalog — same shape as real BADIE products
brands = ["QUILMES", "BRAHMA", "STELLA", "HEINEKEN", "CCU", "BUDWEISER",
          "COCA COLA", "PEPSI", "SPRITE", "FANTA", "DANONE", "VILLAVICENCIO"]
generics = ["CERVEZAS", "GASEOSAS", "AGUAS", "JUGOS", "VINOS"]
calibres = ["RETORNABLE 1000", "LATA 473", "DESC 2250", "BOTELLA 500", "PACK x6"]

textos = [
    f"Producto {i} | marca: {brands[i % len(brands)]} | "
    f"tipo: {generics[i % len(generics)]} | formato: {calibres[i % len(calibres)]}"
    for i in range($SKU_COUNT)
]

print(f"  Encoding {len(textos)} texts...")
t0 = time.time()
vectors = model.encode(textos, batch_size=$BATCH_SIZE, show_progress_bar=True)
elapsed = time.time() - t0

throughput = len(textos) / elapsed
print()
print(f"  Tiempo total: {elapsed:.1f}s")
print(f"  Velocidad: {throughput:.1f} textos/seg")
print()
print("  Estimaciones para tu hardware:")
for n in (1000, 3000, 5000, 10000):
    print(f"    {n:>6} SKUs → {n/throughput:6.1f}s ({n/throughput/60:.1f} min)")
PYEOF

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

title "Done ✓"
echo
echo "  Model cached at: ~/.cache/huggingface/hub/models--BAAI--bge-m3"
echo "  Preflight venv:  $VENV_DIR (delete with: rm -rf $WORK_DIR)"
echo "  GPU mode:        $GPU_TYPE"
echo
echo "  Next: tell Claude to wire the LocalBGEEmbeddingProvider into the project (Paso 1A.4e)."
echo
