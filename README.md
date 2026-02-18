# Tachyon Firmware Updater

Automated firmware updates for Tachyon wireless networks. Handles scheduling, gradual rollouts, and safety checks so you don't have to update APs manually.

## The Problem

Managing firmware updates across dozens or hundreds of Tachyon APs is time-consuming:
- Each AP takes 5-10 minutes to update manually
- Updates require checking weather conditions, verifying system time, and setting bank mode correctly
- One mistake can take down a tower site
- Updates get delayed or skipped because nobody has time to babysit the process

This tool automates the entire process with built-in safety checks and gradual rollouts.

![Dashboard — device table with signal health and update status](docs/screenshots/dashboard.png)

![Auto-update configuration — schedule, firmware, and rollout settings](docs/screenshots/auto-update-config.png)

## Safety Mechanisms

The scheduler includes several safety checks:
- **Temperature validation** - Blocks updates if temperature is below threshold (default 0°C/32°F)
- **System time validation** - Blocks updates if AP clock is unreliable (prevents boot loops)
- **Gradual rollout** - Updates 1 AP (canary), then 10%, 50%, 100% on consecutive nights
- **Automatic pause on failure** - Any failed update stops the rollout for manual review
- **Maintenance windows** - Updates only run on specified days/times
- **Dry-run mode** - Preview what would be updated before enabling the scheduler

## How It Works

1. Upload firmware files and configure maintenance window (e.g., Sundays 2-6 AM)
2. Enable the scheduler
3. The system automatically updates APs over 4 consecutive maintenance windows:
   - Night 1: 1 AP (canary test)
   - Night 2: 10% of remaining APs
   - Night 3: 50% of remaining APs
   - Night 4: All remaining APs

Failures pause the rollout until manually reviewed.

## Supported Devices

**Tachyon Networks**: TNA-301, TNA-302, TNA-303x, TNA-303L, TNA-303L-65, TNS-100

## Additional Features

- **Manual updates** - Immediate updates for specific APs when needed
- **Network topology view** - Visual map of tower sites, APs, and CPEs with signal health indicators
- **Parallel updates** - Configurable concurrency for faster bulk updates
- **Git backups** - Automatic commit of configuration changes
- **Real-time progress** - WebSocket-based live update status

## Quick Start

Both options below run in **standalone mode** — the app plus a bundled nginx reverse proxy with automatic HTTPS (self-signed on first boot, Let's Encrypt via the setup wizard).

### Production Deployment

```bash
curl -sSL https://raw.githubusercontent.com/isolson/firmware-updater/main/scripts/install.sh | sudo bash
```

Installs Docker, configures HTTPS, generates credentials, and starts the system.

Visit `https://your-server` to complete the setup wizard:
1. Change default password
2. Configure Let's Encrypt (optional)
3. Configure git backups (optional)

### Local Testing

```bash
git clone https://github.com/isolson/firmware-updater.git
cd firmware-updater
./deploy.sh
```

Access at `https://localhost` (accept self-signed certificate).

### Without Bundled Nginx

To run behind your own reverse proxy, or access the admin directly on port 8000:

```bash
docker compose up -d --build
```

This starts just the app on port 8000 — no bundled nginx or certbot.

See [docs/deployment.md](docs/deployment.md) for full deployment options.

## Usage

### Automatic Updates

1. Upload firmware files (**Firmware** tab)
2. Configure scheduler (**Auto-Update** tab):
   - Set maintenance window (days and time range)
   - Assign firmware to device models
   - Set temperature threshold (default: 0°C / 32°F)
   - Enable scheduler

The system runs the gradual rollout automatically. Check **Rollout Status** to monitor progress.

See [docs/gradual-rollout.md](docs/gradual-rollout.md) for rollout details.

### Manual Updates

1. Upload firmware (**Firmware** tab)
2. Enter IP addresses (**Update** tab)
3. Configure concurrency and bank mode
4. Start update and monitor progress

Useful for emergency updates, testing new firmware, or updating specific sites outside the schedule.

### Network Monitoring

The **Monitor** page displays network topology (tower sites → APs → CPEs) with signal health indicators. Background polling keeps data current.

## Documentation

- **[Deployment Guide](docs/deployment.md)** - HTTPS, RADIUS, environment variables
- **[Gradual Rollout](docs/gradual-rollout.md)** - How the 4-night rollout works
- **[API Reference](docs/api.md)** - REST endpoints and WebSocket protocol
- **[Architecture](docs/architecture.md)** - System design and data flow

## API Integration

Key endpoints for automation/monitoring:
- `POST /api/start-update` - Trigger manual update
- `GET /api/scheduler/status` - Check scheduler state
- `GET /api/rollout/current` - Get rollout progress
- `WebSocket /ws` - Real-time updates

Full API docs: [docs/api.md](docs/api.md)

## Roadmap

- **Pre-update device config backup** — Pull and store AP/CPE configuration (SSIDs, channels, IP addressing, etc.) before firmware updates. The git backup system currently backs up the management database and device inventory, but not the operational configs on each device. This would enable automatic restore if a firmware update resets device settings to defaults.
- **Git backup restore** — Add a restore flow (API + UI) to pull the latest backup from the configured git remote and replace the local database. Currently restore is a manual process (clone repo, copy `tachyon.db`, restart app).

## For Developers

```bash
# Local dev with auto-reload
uvicorn updater.app:app --reload --port 8000

# Run tests
pytest -v
```

## Summary

For networks with more than a few APs, automated updates are significantly more efficient than manual processes. Initial setup takes about 15 minutes. After that, firmware updates happen automatically according to your schedule.

## License

MIT
