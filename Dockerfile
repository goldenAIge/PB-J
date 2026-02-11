# =============================================================================
# Polymarket Trading Bot - Docker Image
# =============================================================================
#
# Build:
#   docker build -t polymarket-bot .
#
# Run (dry-run mode):
#   docker run -d --name polybot --env-file .env polymarket-bot
#
# Run with specific trader:
#   docker run -d --name polybot-dir --env-file .env polymarket-bot agents.application.arbitrage_trader --dry-run
#   docker run -d --name polybot-scalp --env-file .env polymarket-bot agents.application.resolution_scalper --dry-run
#
# =============================================================================

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    python-dotenv pydantic httpx web3 \
    py-clob-client \
    python-telegram-bot newsapi-python \
    langchain langchain-openai chromadb typer devtools \
    websockets

# Copy application code
COPY . .

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Create logs directory
RUN mkdir -p /app/logs

# Default command (resolution scalper in dry-run)
ENTRYPOINT ["python", "-m"]
CMD ["agents.application.resolution_scalper", "--dry-run", "--scan-interval", "120"]
