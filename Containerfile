# Digest'e PINLI: `:3.10-slim` etiketi upstream'de yeniden yayimlanabilir ve
# ayni Containerfile farkli bir taban imajla derlenebilir. Digest icerigi
# sabitler. Yenilerken: podman pull docker.io/library/python:3.10-slim &&
# podman images --digests docker.io/library/python
FROM docker.io/library/python:3.10-slim@sha256:4101e4a31b9d2c927c9052ae2fa58b76a89265997ec9209c02933d9974214b80

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch

WORKDIR /app

# tesseract: api motorunun OCR yedegi (metin katmani olmayan slaytlar).
# tur paketi olmadan Turkce tanima yapilamaz.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ffmpeg \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-tur \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
COPY requirements-local.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY src ./src
COPY tools ./tools

# DIKKAT: bu imaj KASITLI OLARAK YALINDIR. Lokal motorun agir bagimliliklari
# (torch/transformers/easyocr/onnxruntime) ve model agirliklari imaja GIRMEZ;
# `podcast-models` hacminde dururlar ve `PODCAST_LOCAL_VENV` onlari isaret eder.
# Kurulum imaj DEGISTIRMEDEN, tek komutla yapilir: deploy/setup-local-engine.sh
# (ayni imaji calistirir, boylece tekerlekler runtime Python ABI'siyle uyusur).
# `/app/pipeline` dizini ve sembolik baglar KOSULSUZ kurulur: agac yoksa baglar
# sarkan kalir (zararsiz), yalnizca lokal motor onlari takip eder.
RUN useradd --create-home --uid 10001 podcast \
    && mkdir -p /models/hf /models/torch \
        /data/podcast/jobs /data/podcast/out /data/podcast/kayit \
        /data/podcast/out/_hat /home/podcast/.EasyOCR \
        /app/pipeline/data /app/pipeline/tools/bin \
    && rm -f /app/pipeline/tools/bin/ffmpeg.exe /app/pipeline/tools/bin/ffprobe.exe \
    && ln -sf /usr/bin/ffmpeg /app/pipeline/tools/bin/ffmpeg.exe \
    && ln -sf /usr/bin/ffprobe /app/pipeline/tools/bin/ffprobe.exe \
    && rm -rf /app/pipeline/data/ses_modelleri \
    && ln -sf /models/ses_modelleri /app/pipeline/data/ses_modelleri \
    && rm -rf /app/pipeline/out \
    && ln -sf /data/podcast/out/_hat /app/pipeline/out \
    && chown -R podcast:podcast /app /data/podcast /models /home/podcast

USER podcast

CMD ["python", "-m", "src.main", "--validate"]
