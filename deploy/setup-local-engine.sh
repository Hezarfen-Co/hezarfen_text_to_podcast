#!/usr/bin/env bash
# Lokal motorun bagimliliklarini ve model agirliklarini HACME kurar — imaji
# DEGISTIRMEZ, workflow KOSTURMAZ. Tek komut, yeniden kosulabilir (idempotent).
#
#   bash deploy/setup-local-engine.sh [HACIM] [IMAJ]
#   varsayilan: HACIM=podcast-models  IMAJ=localhost/hezarfen-podcast:current
#
# Neden imaji calistirir: derlenmis tekerlekler (torch/onnxruntime/easyocr)
# runtime'daki ayni Python ABI'siyle kurulmak ZORUNDA. Host'un python3'u ile
# kurmak ABI uyusmazligi uretir.
#
# HACIM ICERIGI:
#   /models/local-venv/        pip --target cikisi (PODCAST_LOCAL_VENV burayi gosterir)
#   /models/ses_modelleri/...  TTS/ASR agirliklari (Hat `ses/ayar.py` sabit yolu)
#   /models/hf, /models/torch  HuggingFace/torch onbellekleri
#
# DIKKAT (durust sinir): bu betik lokal motorun BAGIMLILIKLARINI kurar, ama
# lokal motorun KENDI kaynagini (vendored `router/hat.py` agaci) kuramaz —
# o agac artik hicbir repoda yok (eski Windows kopyasi C:\PROJECTS\podcast ile
# birlikte kayboldu). Yani `PODCAST_ENGINE=local` secilebilir ve bu hacimle
# bagimliliklari hazir olur, ama bugun KOSULAMAZ; PODCAST_PIPELINE_PATH gercek
# bir Hat agacini gostermedikce acilista isimli bir hatayla reddeder.
set -euo pipefail

VOLUME="${1:-podcast-models}"
IMAGE="${2:-localhost/hezarfen-podcast:current}"

if ! command -v podman >/dev/null 2>&1; then
  echo "podman bulunamadi (dnf install podman podman-compose)" >&2
  exit 1
fi

if ! podman image exists "$IMAGE"; then
  echo "imaj yok: $IMAGE — once deploy edin ya da: podman build -f Containerfile -t $IMAGE ." >&2
  exit 1
fi

podman volume exists "$VOLUME" || podman volume create "$VOLUME" >/dev/null
echo "hacim: $VOLUME   imaj: $IMAGE"

podman run --rm --network host \
  -v "$VOLUME:/models" \
  "$IMAGE" \
  bash -euo pipefail -c '
    if [ -f /models/local-venv/.kurulum-tamam ] && [ "${FORCE:-0}" != "1" ]; then
      echo "[local-engine] venv zaten kurulu (/models/local-venv); FORCE=1 ile yenilenir"
    else
      echo "[local-engine] tekerlekler /models/local-venv icine kuruluyor"
      python -m pip install --no-cache-dir --upgrade \
        --target /models/local-venv \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        --requirement /app/requirements-local.txt
      date -u +"%Y-%m-%dT%H:%M:%SZ" > /models/local-venv/.kurulum-tamam
    fi

    if [ -d /models/ses_modelleri/supertonic-3/onnx ]; then
      echo "[local-engine] supertonic-3 agirliklari zaten var"
    else
      echo "[local-engine] supertonic-3 agirliklari indiriliyor"
      PYTHONPATH=/models/local-venv python -c "import huggingface_hub, sys; huggingface_hub.snapshot_download(\"Supertone/supertonic-3\", local_dir=\"/models/ses_modelleri/supertonic-3\")"
    fi

    echo "[local-engine] hacim ozeti:"
    du -sh /models/local-venv /models/ses_modelleri 2>/dev/null || true
  '

cat <<'NOT'

SIRADAKI ADIM (elle): lokal motorun KENDI kaynagi gerekir — vendored Hat agaci.
O agac hicbir repoda YOK (eski kopya C:\PROJECTS\podcast ile kayboldu), yani
PODCAST_ENGINE=local bugun kurulabilir ama KOSULAMAZ. Agac bir gun geri
gelirse: PODCAST_PIPELINE_PATH=<agac koku> ve PODCAST_LOCAL_VENV=/models/local-venv.
NOT
