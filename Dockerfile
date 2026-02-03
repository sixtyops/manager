FROM python:3.12-slim

WORKDIR /app

# Install curl, ping, git, and ssh for backups and device communication
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iputils-ping \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -r -s /bin/false appuser

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY updater/ ./updater/
COPY static/ ./static/

# Create directories for uploads, data, backups, and SSH with restricted permissions
RUN mkdir -p /app/firmware /app/data /app/backups /app/.ssh /app/nginx-conf \
    && chown -R appuser:appuser /app/firmware /app/data /app/backups /app/.ssh /app/nginx-conf \
    && chmod 700 /app/data /app/.ssh

# Expose port
EXPOSE 8000

# Run as non-root user
USER appuser

# Run the application
CMD ["python", "-m", "updater.app"]
