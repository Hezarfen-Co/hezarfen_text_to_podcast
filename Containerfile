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
RUN pip install --no-cache-dir --requirement requirements.txt

# Hat kaynagi OPSIYONELDIR (varsayilan motor `api`, vendor/pipeline olmadan
# derlenir ve kosar). Yalnizca checkout edilmisse lokal motorun agir
# bagimliliklari kurulur; placeholder `.gitkeep` ile bu blok ATLANIR.
COPY vendor /vendor-src
RUN set -eux; \
    if [ -f /vendor-src/pipeline/router/hat.py ]; then \
        mkdir -p /app/pipeline; \
        cp -a /vendor-src/pipeline/. /app/pipeline/; \
        pip install --no-cache-dir \
            --requirement /vendor-src/pipeline/requirements.txt \
            --requirement /vendor-src/pipeline/requirements-ses.txt; \
        pip install --no-cache-dir \
            onnxruntime==1.23.2 \
            numpy==2.2.6 \
            huggingface_hub==1.27.0; \
        if [ "$KUR_AGIR" = "1" ]; then \
            pip install --no-cache-dir \
                --index-url "$TORCH_INDEX" \
                --extra-index-url https://pypi.org/simple \
                torch torchvision; \
            pip install --no-cache-dir transformers==5.15.0; \
            pip install --no-cache-dir --requirement /vendor-src/pipeline/requirements-ocr.txt; \
        else \
            echo "KUR_AGIR=0 -> torch / transformers / easyocr ATLANDI"; \
        fi; \
    else \
        echo "vendor/pipeline yok -> lokal motor bagimliliklari ATLANDI (api motoru kosar)"; \
    fi; \
    rm -rf /vendor-src

COPY src ./src
COPY tools ./tools

RUN if [ -f /app/pipeline/router/hat.py ]; then \
        python tools/dependency_scanner.py /app/pipeline; \
    else \
        echo "hat kaynagi yok -> bagimlilik taramasi ATLANDI"; \
    fi


# /app/pipeline dizini ve sembolik baglar KOSULSUZ kurulur: agac yoksa baglar
# sarkan kalir (zararsiz), yalnizca lokal motor onlari takip eder. Boylece
# `simulate` ve `PODCAST_ENGINE=api` hicbir zaman build/run hatasi vermez;
# `PODCAST_MODE=real` + `PODCAST_ENGINE=local` ise CALISMA ANINDA yuksek sesle
# reddeder (bkz. src/pipeline.py:check_pipeline_path).
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
