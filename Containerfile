# Digest'e PINLI: `:3.10-slim` etiketi upstream'de yeniden yayimlanabilir ve
# ayni Containerfile farkli bir taban imajla derlenebilir. Digest icerigi
# sabitler. Yenilerken: podman pull docker.io/library/python:3.10-slim &&
# podman images --digests docker.io/library/python
FROM docker.io/library/python:3.10-slim@sha256:4101e4a31b9d2c927c9052ae2fa58b76a89265997ec9209c02933d9974214b80

ARG KUR_AGIR=1
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    PYTHONPATH=/app/pipeline

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ffmpeg \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY vendor/pipeline/requirements.txt ./hat-req/requirements.txt
COPY vendor/pipeline/requirements-ses.txt ./hat-req/requirements-ses.txt
COPY vendor/pipeline/requirements-ocr.txt ./hat-req/requirements-ocr.txt

RUN pip install --no-cache-dir \
        --requirement hat-req/requirements.txt \
        --requirement hat-req/requirements-ses.txt \
    && pip install --no-cache-dir \
        onnxruntime==1.23.2 \
        numpy==2.2.6 \
        huggingface_hub==1.27.0

RUN if [ "$KUR_AGIR" = "1" ]; then \
        pip install --no-cache-dir \
            --index-url "$TORCH_INDEX" \
            --extra-index-url https://pypi.org/simple \
            torch torchvision \
        && pip install --no-cache-dir transformers==5.15.0 \
        && pip install --no-cache-dir --requirement hat-req/requirements-ocr.txt ; \
    else \
        echo "KUR_AGIR=0 -> torch / transformers / easyocr ATLANDI" ; \
    fi

COPY vendor/pipeline /app/pipeline
COPY src ./src
COPY tools ./tools

RUN python tools/dependency_scanner.py /app/pipeline


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
