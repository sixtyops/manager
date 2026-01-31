# Tachyon Management System

Web-based firmware update tool for production wireless network devices. Supports parallel updates with real-time progress monitoring, automatic scheduling with gradual rollout, and network topology visualization.

## Supported Devices

- **Tachyon Networks** - TNA-301, TNA-302, TNA-303x, TNA-303L, TNA-303L-65, TNS-100
- **MikroTik** - Planned

## Features

- **Web UI** with real-time WebSocket progress updates
- **Parallel firmware updates** with configurable concurrency
- **Automatic scheduling** with maintenance windows (day-of-week + time range)
- **Gradual rollout** - canary → 10% → 50% → 100% across consecutive nights
- **Network topology monitoring** - background polling of APs and CPEs with signal health
- **Weather & time safety checks** - blocks updates when temperature is too low or system clock is unreliable
- **Authentication** - RADIUS with local fallback, session-based
- **Tower site management** - organize APs by physical location
- **Docker deployment** with persistent volumes

## Quick Start

### Docker (recommended)

```bash
docker compose up -d
```

Open http://localhost:8000 and log in with `admin` / `changeme`.

### Local Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .

# Set credentials
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD=changeme

# Start server
firmware-updater
```

See [docs/deployment.md](docs/deployment.md) for full configuration options.

## Usage

### Manual Update

1. Go to the **Firmware** tab and upload firmware files
2. Go to the **Update** tab
3. Paste IP addresses (one per line), set concurrency and bank mode
4. Click **Start Update** and monitor real-time progress

### Automatic Updates

1. Go to the **Auto-Update** tab
2. Enable the schedule and select days/hours for the maintenance window
3. Select firmware files for your device models
4. The scheduler runs a gradual rollout: 1 AP on night 1 (canary), then 10%, 50%, and 100% on subsequent nights
5. Any failure pauses the rollout for manual review

See [docs/gradual-rollout.md](docs/gradual-rollout.md) for rollout details.

### Network Monitoring

The **Monitor** page shows a live topology of tower sites, access points, and connected CPEs with signal health indicators (green/yellow/red based on RX power).

## API

See [docs/api.md](docs/api.md) for the full endpoint reference. Key endpoints:

| Endpoint | Description |
|----------|-------------|
| `POST /api/start-update` | Start a firmware update job |
| `GET /api/topology` | Get network topology |
| `GET /api/scheduler/status` | Get scheduler and rollout state |
| `GET /api/rollout/current` | Get active rollout progress |
| `WebSocket /ws` | Real-time status updates |

## Architecture

See [docs/architecture.md](docs/architecture.md) for system design details.

## Development

```bash
# Run with auto-reload
uvicorn updater.app:app --reload --port 8000

# Run tests
pytest -v
```

## License

MIT
