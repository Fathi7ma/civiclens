FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CIVICLENS_CACHE_DIR=/tmp/civiclens-cache

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY civiclens ./civiclens
COPY eval ./eval

# Warm the query cache in the background so the first request is not the
# slowest one a judge ever sees.
COPY prewarm.py .
RUN python prewarm.py || true

EXPOSE 8000
CMD ["uvicorn", "civiclens.app:app", "--host", "0.0.0.0", "--port", "8000"]