# SixtyOps Manager

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
- **Gradual rollout** - Updates pinned canary APs / switches first, then 10%, 50%, 100% on consecutive windows
- **Manual canary run** - The pending Canary pill can run the test canary outside the normal maintenance window
- **Automatic pause on failure** - Any failed update stops the rollout for manual review
- **Maintenance windows** - Updates only run on specified days/times
- **Dry-run mode** - Preview what would be updated before enabling the scheduler

## How It Works

1. Upload firmware files and configure maintenance window (e.g., Sundays 2-6 AM)
2. Enable the scheduler
3. Optionally pin test APs and switches as dedicated canaries in the firmware drawer
4. The system automatically updates the fleet over 4 consecutive maintenance windows:
   - Window 1: Canary APs (+ attached CPEs) and canary switches
   - Window 2: 10% of remaining APs and switches
   - Window 3: 50% of remaining APs and switches
   - Window 4: All remaining APs and switches

Failures pause the rollout until manually reviewed.

## Supported Devices

**Tachyon Networks**: TNA-301, TNA-302, TNA-303x, TNA-303L, TNA-303L-65, TNS-100

## Additional Features

- **Manual updates** - Immediate updates for specific APs when needed
- **Network topology view** - Visual map of tower sites, APs, and CPEs with signal health indicators
- **Parallel updates** - Configurable concurrency for faster bulk updates
- **Built-in device RADIUS** - Integrated RADIUS server for AP and switch admin authentication
- **SFTP backups** - Automated backup of database and device configurations
- **Real-time progress** - WebSocket-based live update status

## Quick Start

Both options below run in **standalone mode** — the app plus a bundled nginx reverse proxy with automatic HTTPS (self-signed on first boot, Let's Encrypt via the setup wizard).

### Production Deployment

```bash
curl -sSL https://raw.githubusercontent.com/sixtyops/manager/main/scripts/install.sh | sudo bash
```

Installs Docker, configures HTTPS, generates credentials, and starts the system.

Visit `https://your-server` to complete the setup wizard:
1. Change default password
2. Configure Let's Encrypt (optional)
3. Configure SFTP backups (optional)

### Local Testing

```bash
git clone https://github.com/sixtyops/manager.git
cd manager
./deploy.sh
```

Access at `https://localhost` (accept self-signed certificate).

### Behind Your Own Reverse Proxy

To run behind your own reverse proxy:

```bash
docker compose up -d --build
```

The app listens on port 8000. The bundled nginx is included but has no published ports — your proxy forwards directly to `localhost:8000`. To expose nginx on custom ports instead (e.g., for the built-in SSL management), add a `docker-compose.override.yml`.

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
If you have a lab AP or switch, pin it as a canary in the firmware drawer. You can also click the pending `Canary` pill to run that test phase before the maintenance window opens.

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

- **[Deployment Guide](docs/deployment.md)** - HTTPS, built-in Radius, environment variables
- **[Radius Guide](docs/radius.md)** - Built-in FreeRADIUS setup, client overrides, and device rollout workflow
- **[Gradual Rollout](docs/gradual-rollout.md)** - How the 4-night rollout works
- **[Release System](docs/release-system.md)** - Branches, tags, workflows, GHCR, appliance publishing, self-update behavior
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

- **Per-device firmware update cooldown** — Prevent rapid re-updating of a device after a successful firmware update (default 30 days). This ensures a stable soak period before applying further changes.
- **Pre-update device config backup** — Automatically pull and store a configuration snapshot before starting a firmware update. This ensures a "before" record exists for every update, enabling quick recovery if an update resets settings to defaults.
- **SFTP backup restore** — Add a restore flow (API + UI) to pull a selected backup from the SFTP server and replace the local database. Currently restore is a manual process.

## For Developers

```bash
# Local dev with auto-reload
uvicorn updater.app:app --reload --port 8000

# Run tests
pytest -v
```

## Development Workflow

All work happens on feature branches off `main`:

### Contributing

1. Create a feature branch from `main`
2. Make changes and run tests (`pytest -v`)
3. Open a PR targeting `main`
4. After merge, tag a dev or stable release as needed

### Release Channels

The app supports two self-update channels (Settings > Updates):
- **Stable** (default) — Only full releases from `main`
- **Dev** — Includes pre-releases for early testing

See [CLAUDE.md](CLAUDE.md) for detailed release procedures.

## Summary

For networks with more than a few APs, automated updates are significantly more efficient than manual processes. Initial setup takes about 15 minutes. After that, firmware updates happen automatically according to your schedule.

## License

[Elastic License 2.0 (ELv2)](LICENSE) — free to use and modify, but you may not offer it as a managed service or repackage it for sale.
