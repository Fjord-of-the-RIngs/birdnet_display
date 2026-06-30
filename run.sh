#!/bin/bash
# This script activates the virtual environment and starts the Flask server.
echo "Starting the Bird Detection Display..."
# Change into the directory where the application is installed.  This script
# assumes the project has been unpacked into /home/birdpi/birdnet_display.  If you
# install it elsewhere, update the path below accordingly.
cd "/home/birdpi/birdnet_display"
source venv/bin/activate
python3 birdnet_display.py
