# Firmware Update Tool

Web-based firmware update tool for production network devices. Supports parallel updates with real-time progress monitoring.

## Supported Devices

- **Tachyon Networks** - TNA-30x series
- **MikroTik** - Coming soon

## Features

- Web UI for easy operation
- Upload firmware files or select from existing
- Paste or upload IP list
- Parallel updates (configurable concurrency)
- Real-time progress via WebSocket
- Per-device status tracking

## Installation

```bash
# Clone the repository
git clone https://github.com/your-org/firmware-updater.git
cd firmware-updater

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Or install as package
pip install -e .
```

## Usage

### Start the server

```bash
# Using the installed command
firmware-updater

# Or run directly
python -m updater.app

# Custom port
PORT=8080 firmware-updater
```

### Access the web UI

Open http://localhost:8000 in your browser.

### Update workflow

1. **Select device type** - Currently Tachyon Networks
2. **Upload firmware** - Drag & drop or click to browse
3. **Enter credentials** - Username and password for all devices
4. **Paste IP list** - One IP per line
5. **Set concurrency** - Number of parallel updates (default: 5)
6. **Click Start Update**

### IP List Format

```
# Comments start with #
192.168.1.10
192.168.1.11
192.168.1.12
```

## API Endpoints

- `GET /` - Web UI
- `POST /api/upload-firmware` - Upload firmware file
- `GET /api/firmware-files` - List available firmware files
- `POST /api/start-update` - Start update job
- `GET /api/job/{job_id}` - Get job status
- `WebSocket /ws` - Real-time updates

## Development

```bash
# Run with auto-reload
uvicorn updater.app:app --reload --port 8000
```

## License

MIT
