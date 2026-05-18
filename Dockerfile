FROM python:3.13-slim

WORKDIR /app

# Copy package metadata first for a cacheable layer.
COPY pyproject.toml /app/
COPY app/ /app/app/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/portal.db

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
