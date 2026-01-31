FROM python:3.12-slim

WORKDIR /app

# Install curl and ping for the TachyonClient
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -r -s /bin/false appuser

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY updater/ ./updater/
COPY static/ ./static/

# Create directories for uploads and data with restricted permissions
RUN mkdir -p /app/firmware /app/data \
    && chown -R appuser:appuser /app/firmware /app/data \
    && chmod 700 /app/data

# Expose port
EXPOSE 8000

# Run as non-root user
USER appuser

# Run the application
CMD ["python", "-m", "updater.app"]
