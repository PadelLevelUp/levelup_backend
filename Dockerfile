# ----------------------------
# Stage 1: Build/install deps
# ----------------------------
FROM python:3.10-slim AS builder
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY padel_app ./padel_app

RUN pip install --upgrade pip setuptools wheel
RUN pip install --prefix=/install .

# ----------------------------
# Stage 2: Runtime image
# ----------------------------
FROM python:3.10-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY . .

ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.py

EXPOSE 80

RUN chmod +x /app/scripts/entrypoint.sh
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

CMD ["python", "app.py"]
