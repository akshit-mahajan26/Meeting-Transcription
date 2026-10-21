FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine/ engine/

RUN useradd --create-home appuser
USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD curl -fsS http://localhost:8001/health || exit 1

CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8001"]
