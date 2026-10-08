FROM python:3.12-slim

WORKDIR /app

# Install curl, ping, git, ssh, and Docker CLI (for self-update via mounted socket)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iputils-ping \
    git \
    openssh-client \
    ca-certificates \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with fixed UID/GID so host-side scripts can set matching ownership on bind-mounted dirs
RUN groupadd -r -g 1500 appuser && useradd -r -u 1500 -g 1500 -m -d /home/appuser -s /bin/false appuser
# Trust only the self-update mount. Runtime startup does not write Git config.
RUN git config --system --add safe.directory /app/repo

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY updater/ ./updater/
COPY static/ ./static/
COPY entrypoint.sh /entrypoint.sh
COPY scripts/container_user.py /container_user.py
RUN chmod +x /entrypoint.sh


# Create directories for uploads, data, backups, and SSH with restricted permissions
RUN mkdir -p /app/firmware /app/data /app/backups /app/.ssh /app/nginx-conf \
    && chown -R appuser:appuser /app/firmware /app/data /app/backups /app/.ssh /app/nginx-conf \
    && chmod 700 /app/data /app/.ssh

# Expose ports
EXPOSE 8000 1812/udp

# Entrypoint sets process groups and drops to appuser without editing /etc.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "updater.app"]
