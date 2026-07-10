#!/bin/bash
# This script activates the virtual environment and starts the Flask server.
echo "Starting the Bird Detection Display..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BIRDNET_DISPLAY_HOME="${BIRDNET_DISPLAY_HOME:-$SCRIPT_DIR}"
INSTALL_PARENT="$(dirname "$BIRDNET_DISPLAY_HOME")"
export BIRDNET_PI_HOME="${BIRDNET_PI_HOME:-$INSTALL_PARENT/BirdNET-Pi}"
export BIRDNET_DB_PATH="${BIRDNET_DB_PATH:-$BIRDNET_PI_HOME/scripts/birds.db}"
export BIRDNET_AUDIO_DIR="${BIRDNET_AUDIO_DIR:-$INSTALL_PARENT/BirdSongs/Extracted/By_Date}"
cd "$BIRDNET_DISPLAY_HOME"
source venv/bin/activate
python3 birdnet_display.py
