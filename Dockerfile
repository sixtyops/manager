FROM python:3.12-slim

WORKDIR /app

# Install curl for the TachyonClient
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY updater/ ./updater/
COPY static/ ./static/

# Create firmware directory for uploads
RUN mkdir -p /app/firmware

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "-m", "updater.app"]
