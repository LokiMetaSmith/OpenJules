FROM python:3.11-slim

# Install system dependencies: git for repository management, curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for caching layers
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV DOCKER_HOST=tcp://dind:2375

EXPOSE 8000

CMD ["uvicorn", "src.api.webhook:app", "--host", "0.0.0.0", "--port", "8000"]
