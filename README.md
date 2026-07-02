# BirdNET Display

A Raspberry Pi touchscreen display for a BirdNET-Pi/BirdNET-Go station. It shows recent bird detections, local bird photos, daily and all-time species views, QR access, system controls, and optional audio clip playback from recorded detections.

This repo tracks the full display project, including the Flask backend (`birdnet_display.py`), cache builder, installer scripts, 3D print files, images, and the web UI in `static/index.html`.

## Credits

This project began from the original BirdNET display work by C4KEW4LK:

https://github.com/C4KEW4LK/birdnet_display

That original project provided the foundation for a Raspberry Pi based BirdNET display with a Flask backend, cached bird images, and a browser-based interface. This repo is my continued build of that display, with changes made over roughly eight months of day-to-day use on my own BirdNET setup.

## What This Version Adds

- Expanded touchscreen interface in `static/index.html`, with multiple display layouts, dark mode, settings panels, all-birds views, all-time views, and search/sort controls.
- Better local image handling, including species folder detection, placeholder images, no-photo states, photo upload, photo browsing, and image deletion from the UI.
- Detection audio support, including latest clip, best clip, spectrogram serving, per-species playback, first-detection playback, and periodic "chirp" style playback from today's detections.
- Bird activity views backed by the local BirdNET database, including recent detections, all species detected today, all-time detections, species stats, and bird-day index endpoints.
- Raspberry Pi hardware controls from the display UI, including screen brightness, reboot, shutdown, fan mode, fan speed, CPU temperature, and hardware fan-state feedback.
- Offline and waiting-for-detections behavior using cached species images or placeholder photos instead of showing a blank screen.

## Screenshots and Build Photos

### Current UI Highlights

Live recent detections on the touchscreen layout:

![Current UI - Main Detections](images/ui-main-detections.png)

Settings panel with layout, display mode, schedule, temperature, fan, and system controls:

![Current UI - Settings](images/ui-settings.png)

Per-species detail panel with latest recording, spectrogram, stats, image folder tools, uploads, and photo browser:

![Current UI - Bird Details](images/ui-bird-details.png)

All birds detected in the last 24 hours, with sorting and search:

![Current UI - All Birds](images/ui-all-birds.png)

All-time species view:

![Current UI - All Time](images/ui-all-time.png)

Bird Day Index analytics:

![Current UI - Bird Day Index](images/ui-bird-day-index.png)

QR access overlay:

![Current UI - QR Overlay](images/ui-qr-overlay.png)

### Completed System

Front:

![System Front](images/system%20front.png)

Side:

![System Side](images/system%20side.png)

Internals:

![System Internals](images/system%20internals.png)

## Features

- Designed for a Raspberry Pi with an attached 800x480 class touchscreen.
- Integrates with a local BirdNET-Pi/BirdNET-Go installation and SQLite detection database.
- Shows recent detections with confidence, species photos, timing, and folder/photo status.
- Provides all-birds and all-time views for reviewing detected species.
- Serves the complete browser UI from `static/index.html`.
- Uses local image caches so the display remains useful offline.
- Lets you create species image folders and upload/manage photos from the web UI.
- Plays locally stored detection clips when available.
- Includes kiosk-mode startup support for a dedicated display.
- Provides brightness, fan, reboot, and poweroff controls from the UI.
- Includes optional, advanced AP setup support for field/local deployments.
- Includes 3D-print files for the display enclosure and microphone housing.

## Setup and Installation

### Automatic Installation

On a Raspberry Pi:

```bash
git clone https://github.com/Fjord-of-the-RIngs/birdnet_display.git
cd birdnet_display
chmod +x install.sh
./install.sh
```

The installer sets up the application directory, Python virtual environment, dependencies, image cache, and optional kiosk/networking pieces.

### Manual Installation

```bash
git clone https://github.com/Fjord-of-the-RIngs/birdnet_display.git
cd birdnet_display
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python cache_builder.py
python birdnet_display.py
```

If `venv` support is missing:

```bash
sudo apt-get install python3-venv
```

## Usage

Run the application manually:

```bash
cd ~/birdnet_display
./run.sh
```

Then open:

```text
http://<your-pi-ip>:5000
```

If kiosk mode is enabled, Chromium launches the display automatically on boot.

## Configuration

Filesystem paths are resolved in `path_config.py`. Environment variables take precedence, and defaults are chosen so a normal Raspberry Pi install under the current Linux user works without extra setup.

| Variable | Default |
| --- | --- |
| `BIRDNET_DISPLAY_HOME` | directory containing `birdnet_display.py` |
| `BIRDNET_DISPLAY_STATIC_DIR` | `$BIRDNET_DISPLAY_HOME/static` |
| `BIRDNET_IMAGE_CACHE_DIR` | `$BIRDNET_DISPLAY_STATIC_DIR/bird_images_cache` |
| `BIRDNET_IMAGE_DIR` | fallback alias for `BIRDNET_IMAGE_CACHE_DIR` |
| `BIRDNET_PI_HOME` | `$HOME/BirdNET-Pi` |
| `BIRDNET_DB_PATH` | `$BIRDNET_PI_HOME/scripts/birds.db` |
| `BIRDNET_AUDIO_DIR` | `$HOME/BirdSongs/Extracted/By_Date` |

Current Raspberry Pi default example for user `birdpi`:

```bash
cd /home/birdpi/birdnet_display
./run.sh
```

The resolved defaults are:

```text
BIRDNET_DISPLAY_HOME=/home/birdpi/birdnet_display
BIRDNET_DB_PATH=/home/birdpi/BirdNET-Pi/scripts/birds.db
BIRDNET_AUDIO_DIR=/home/birdpi/BirdSongs/Extracted/By_Date
BIRDNET_IMAGE_CACHE_DIR=/home/birdpi/birdnet_display/static/bird_images_cache
```

Different username or custom install path example:

```bash
export BIRDNET_DISPLAY_HOME="/opt/birdnet-display"
export BIRDNET_PI_HOME="/home/alex/BirdNET-Pi"
export BIRDNET_AUDIO_DIR="/home/alex/BirdSongs/Extracted/By_Date"
cd "$BIRDNET_DISPLAY_HOME"
./run.sh
```

Override only the database or image cache:

```bash
export BIRDNET_DB_PATH="/mnt/birdnet/scripts/birds.db"
export BIRDNET_IMAGE_CACHE_DIR="/mnt/birdnet-display-cache"
./run.sh
```

### Admin controls

Admin/destructive controls are disabled until an admin secret is configured. Set a long random value before using reboot, poweroff, brightness, fan changes, image upload, image delete, or image folder creation:

```bash
export BIRDNET_DISPLAY_ADMIN_SECRET="replace-with-a-long-random-value"
```

If you want Flask sessions signed with a separate key, also set:

```bash
export BIRDNET_DISPLAY_FLASK_SECRET_KEY="replace-with-another-long-random-value"
```

To restrict admin actions to loopback and trusted LAN ranges in addition to login and CSRF protection:

```bash
export ADMIN_REQUIRE_LOCAL_NETWORK=true
export TRUSTED_ADMIN_NETWORKS="127.0.0.0/8,::1/128,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
```

The browser dashboard prompts for the admin secret on the first protected action. The secret is sent only to `/admin/login` and is not stored in localStorage.

Edit `species_list.csv` to control which species are used for offline/cache building, then rebuild:

```bash
cd ~/birdnet_display
source venv/bin/activate
python cache_builder.py
```

## Optional BirdNET-Go Networking

The main installer does not modify BirdNET-Go, stop BirdNET-Go, rewrite its service file, or remove any BirdNET-Go systemd drop-ins.

If you need BirdNET-Go to run with Docker host networking, use the advanced script after install. Review the dry run first:

```bash
sudo ~/birdnet_display/scripts/configure_birdnet_go_networking.sh --dry-run
```

Apply the change only after reviewing the generated drop-in:

```bash
sudo ~/birdnet_display/scripts/configure_birdnet_go_networking.sh --apply
```

If your BirdNET-Go service has a different name:

```bash
sudo ~/birdnet_display/scripts/configure_birdnet_go_networking.sh --service birdnet-go --dry-run
```

The script creates or updates only this BirdNET Display-owned drop-in:

```text
/etc/systemd/system/birdnet-go.service.d/birdnet-display-networking.conf
```

Backups of files the script touches are stored under:

```text
/var/backups/birdnet-display/birdnet-go-networking/YYYYMMDD-HHMMSS/
```

Inspect the effective service:

```bash
systemctl cat birdnet-go
```

Manual revert:

```bash
sudo rm /etc/systemd/system/birdnet-go.service.d/birdnet-display-networking.conf
sudo systemctl daemon-reload
sudo systemctl restart birdnet-go
```

## Project Structure

```text
.
├── 3d print files/          # 3D printable enclosure and microphone housing files
├── images/                  # README screenshots and build photos
├── static/
│   └── index.html           # Main touchscreen/browser UI
├── ap_setup.sh              # Optional Wi-Fi access point setup helper
├── ap_setup.example.conf    # Placeholder AP setup config example
├── birdnet_display.py       # Main Flask backend
├── cache_builder.py         # Local bird image cache builder
├── install.sh               # Raspberry Pi installer
├── kiosk_launcher.sh        # Chromium kiosk launcher
├── scripts/
│   └── configure_birdnet_go_networking.sh
├── requirements.txt         # Python dependencies
├── run.sh                   # App runner
└── species_list.csv         # Species list for cache building
```

## Access Point Setup

`ap_setup.sh` can configure the Raspberry Pi as a Wi-Fi access point for deployments without a normal network. This is optional and advanced: it changes NetworkManager configuration on the Pi.

The script has no active default SSID, password, interface, MAC address, or fixed client IP. It refuses to change networking unless you explicitly use `--apply`.

Preview the change first:

```bash
./ap_setup.sh --dry-run \
  --ssid "YourDisplayAP" \
  --password "change-this-password" \
  --interface wlan1
```

Apply after reviewing the summary:

```bash
sudo ./ap_setup.sh --apply \
  --ssid "YourDisplayAP" \
  --password "change-this-password" \
  --interface wlan1
```

You can also copy the example config and fill in your own values:

```bash
cp ap_setup.example.conf ap_setup.conf
./ap_setup.sh --dry-run --config ap_setup.conf
sudo ./ap_setup.sh --apply --config ap_setup.conf
```

The script creates or updates only the configured NetworkManager connection name, defaulting to `BirdNET-Display-AP`. It does not delete unrelated connections, edit `NetworkManager.conf`, write dnsmasq shared config, or restart NetworkManager.

Inspect or remove the created AP connection:

```bash
nmcli connection show "BirdNET-Display-AP"
sudo nmcli connection delete "BirdNET-Display-AP"
```

## 3D Printed Files

This repo includes 3D-print files for the main Raspberry Pi/display housing and ESP32 microphone housing. The files include options for direct screws or heat-set threaded inserts.

Main build hardware used:

- Raspberry Pi 4B.
- 5 inch DSI touchscreen.
- Panel-mount USB-C connector.
- GeeekPi Armor Lite style Raspberry Pi 4 heatsink/fan.
- M2 and M2.5 screws plus optional heat-set inserts.

## Troubleshooting

- If the UI does not load, confirm `static/index.html` exists beside `birdnet_display.py`.
- If bird photos do not appear, rebuild the cache with `python cache_builder.py`.
- If the app does not start on boot, check the service status and journal logs.
- If audio clips do not play, confirm the extracted BirdNET audio files exist under `BIRDNET_AUDIO_DIR`.
- If fan or brightness controls fail, confirm the Raspberry Pi hardware paths and sudo permissions match this setup.

## Admin Security Checks

Manual checks on the Raspberry Pi:

- Without `BIRDNET_DISPLAY_ADMIN_SECRET`, `curl -X POST http://127.0.0.1:5000/reboot` should return an admin-disabled error and must not reboot.
- With the secret configured, unauthenticated `POST /reboot` should return `401`.
- In the dashboard, a protected action should prompt for the admin secret.
- An authenticated `POST /reboot` should work only when the request includes the session cookie and `X-CSRF-Token`.
- Missing or invalid `X-CSRF-Token` should return `403`.
- Public display pages such as `/`, `/data`, `/temp`, and `GET /api/bird_images` should still load without login.
- Uploading a non-image file or a renamed text file should be rejected.
- Creating an image folder named `../test` should be rejected.
- Image upload and delete should stay limited to `static/bird_images_cache`.

## Image Upload Checks

Manual checks on the Raspberry Pi:

- Upload a valid JPEG from the bird detail panel and confirm it appears in the photo browser.
- Upload a valid PNG and confirm it appears in the photo browser.
- Rename a text file to `fake.jpg` and confirm upload is rejected.
- Upload a file with an unsupported extension and confirm upload is rejected.
- Upload a file larger than `BIRDNET_UPLOAD_MAX_BYTES` and confirm a clean size error.
- Upload an image wider or taller than `4096px`, or over `8,847,360` total pixels, and confirm upload is rejected.
- Upload the same original filename twice and confirm both generated files remain.
- Upload a filename like `../test.jpg` and confirm it cannot escape the image directory.
- Confirm existing bird image display behavior still works after upload.

## Path Configuration Checks

Manual checks on the Raspberry Pi:

- Run from `/home/birdpi/birdnet_display` with no environment variables and confirm startup logs show the expected `/home/birdpi` defaults.
- Copy or clone the display project under another user or path and confirm startup logs show that clone as `BIRDNET_DISPLAY_HOME`.
- Set `BIRDNET_DB_PATH` to a test SQLite database and confirm database warnings/API reads reference that exact file.
- Set `BIRDNET_IMAGE_CACHE_DIR` to a temporary directory, run `python cache_builder.py`, and confirm images are written there.
- Set `BIRDNET_AUDIO_DIR` to a temporary extracted-audio root and confirm clip lookup checks that location.
- Run `rg -n "/home/birdpi|birdpi" .` and confirm any matches are documentation examples, not executable app assumptions.
- Start the app normally and confirm it can find database records, audio files, and bird image cache files.

## BirdNET-Go Networking Checks

Manual checks on the Raspberry Pi:

- Run `./install.sh` and confirm it does not prompt to configure BirdNET-Go networking.
- Confirm `install.sh` does not stop, restart, delete, or rewrite BirdNET-Go systemd files.
- Run `sudo ~/birdnet_display/scripts/configure_birdnet_go_networking.sh --dry-run` and confirm it prints the target drop-in without changing files or restarting services.
- Run `sudo ~/birdnet_display/scripts/configure_birdnet_go_networking.sh --apply` and confirm it creates only `/etc/systemd/system/birdnet-go.service.d/birdnet-display-networking.conf`.
- Confirm unrelated files in `/etc/systemd/system/birdnet-go.service.d/` remain in place.
- Confirm a timestamped backup directory is created before replacing an existing BirdNET Display-owned drop-in.
- Confirm the script exits safely if `birdnet-go.service` is not installed.
- Confirm the script exits safely without sufficient permissions when using `--apply`.
- Remove the created drop-in, run `sudo systemctl daemon-reload`, and restart BirdNET-Go to restore prior behavior.
- Run `systemctl cat birdnet-go` and confirm the main service is unchanged plus the BirdNET Display-owned drop-in only.

## Repository

https://github.com/Fjord-of-the-RIngs/birdnet_display

## License

MIT License. Original project credit belongs to C4KEW4LK; this repo contains my ongoing modifications and additions.
