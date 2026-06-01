# Reading with Chinese -- container image.
#
# This app is NOT suitable for serverless/Lambda platforms (e.g. Vercel
# Functions): its dependency tree is >1 GB, it runs multi-minute background
# translation jobs, and it keeps job state + files on a local disk. It needs a
# long-lived container. This image runs on Render, Railway, Fly.io, Koyeb,
# Hugging Face Spaces (Docker), or any plain VM/VPS.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PORT=8000

WORKDIR /app

# Runtime shared libs needed by onnxruntime (libgomp) and the image stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install deps, then trim two things pdf2zh pulls in transitively but this app
# never uses, to shrink the image substantially:
#   * opencv-python  -> the GUI build (needs libGL); the headless build that is
#                       also installed provides the same `cv2` and is enough.
#   * gradio*        -> pdf2zh's own web UI; we ship our own frontend instead.
RUN pip install -r requirements.txt \
    && pip uninstall -y opencv-python opencv-python-headless gradio gradio-client gradio-pdf || true \
    && pip install opencv-python-headless

COPY . .

EXPOSE 8000

# Respect the platform-provided $PORT (Render/Railway/etc.), default to 8000.
CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
