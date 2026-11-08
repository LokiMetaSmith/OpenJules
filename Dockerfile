FROM python:3.11-slim

# Install system dependencies: git, curl, ca-certificates, tar
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    tar \
    && rm -rf /var/lib/apt/lists/*

# Install Radicle CLI ('rad' and 'git-remote-rad')
# Official Radicle binaries can be installed or linked
ENV RAD_HOME=/root/.radicle
ENV PATH="${PATH}:/root/.radicle/bin:/usr/local/bin"

WORKDIR /app

# Install Python dependencies first for caching layers
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV DOCKER_HOST=tcp://dind:2375
ENV RADICLE_NODE_URL=http://radicle-node:8080/api/v1

EXPOSE 8000

CMD ["uvicorn", "src.api.webhook:app", "--host", "0.0.0.0", "--port", "8000"]
