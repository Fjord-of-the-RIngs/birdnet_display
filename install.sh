#!/bin/bash
set -e

# --- Configuration ---
INSTALL_DIR_NAME="birdnet_display"
INSTALL_DIR="${BIRDNET_DISPLAY_HOME:-$HOME/$INSTALL_DIR_NAME}" # Use the current user's home directory for the installation by default
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPLAY_STATIC_DIR="${BIRDNET_DISPLAY_STATIC_DIR:-$INSTALL_DIR/static}"
CACHE_DIR_RELATIVE="static/bird_images_cache"
SOURCE_CACHE_DIR="$SOURCE_DIR/$CACHE_DIR_RELATIVE"
INSTALL_CACHE_DIR="${BIRDNET_IMAGE_CACHE_DIR:-${BIRDNET_IMAGE_DIR:-$DISPLAY_STATIC_DIR/bird_images_cache}}"
REBOOT_REQUIRED=false

# Color Codes for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}--- Starting Bird Detection Display Standalone Setup ---${NC}"
echo -e "${YELLOW}Installation Target: $INSTALL_DIR${NC}"
echo -e "${YELLOW}Source Directory: $SOURCE_DIR${NC}"

# --- Create Install Directory ---
echo -e "\n${YELLOW}Step 1: Creating installation directory...${NC}"
mkdir -p "$INSTALL_DIR"
echo -e "${GREEN}✅ Directory created at $INSTALL_DIR${NC}"


# --- Step 2: Check for Python and Pip ---
echo -e "\n${YELLOW}Step 2: Checking for Python 3 and Pip...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}ERROR: Python 3 is not installed. Please install Python 3 and try again.${NC}"
    exit 1
fi
if ! python3 -m pip --version &> /dev/null; then
    echo -e "${RED}ERROR: Pip for Python 3 is not installed.${NC}"
    echo -e "${YELLOW}Please install it (e.g., with 'sudo apt-get install python3-pip') and try again.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Python 3 and Pip found.${NC}"

# --- Step 3: Copy Project Files ---
echo -e "\n${YELLOW}Step 3: Copying project files to $INSTALL_DIR...${NC}"
cp "$SOURCE_DIR/birdnet_display.py" "$INSTALL_DIR/"
cp "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/"
cp "$SOURCE_DIR/run.sh" "$INSTALL_DIR/"
cp "$SOURCE_DIR/kiosk_launcher.sh" "$INSTALL_DIR/"
cp "$SOURCE_DIR/cache_builder.py" "$INSTALL_DIR/"
cp "$SOURCE_DIR/path_config.py" "$INSTALL_DIR/"
cp "$SOURCE_DIR/species_list.csv" "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/scripts"
cp "$SOURCE_DIR/scripts/configure_birdnet_go_networking.sh" "$INSTALL_DIR/scripts/"
mkdir -p "$DISPLAY_STATIC_DIR"
cp -r "$SOURCE_DIR/static/index.html" "$DISPLAY_STATIC_DIR/"
mkdir -p "$INSTALL_CACHE_DIR"
if [ -d "$SOURCE_CACHE_DIR" ]; then
    echo -e "${YELLOW}Existing image cache found. Copying cached images...${NC}"
    cp -a "$SOURCE_CACHE_DIR/." "$INSTALL_CACHE_DIR/"
else
    echo -e "${YELLOW}No existing image cache found in source checkout. A fresh cache directory was created and will be populated in Step 7.${NC}"
fi
echo -e "${GREEN}✅ Project files copied.${NC}"


# --- Step 4: Set up Python Virtual Environment ---
echo -e "\n${YELLOW}Step 4: Creating Python virtual environment in $INSTALL_DIR...${NC}"
if ! python3 -m venv "$INSTALL_DIR/venv"; then
    echo -e "${YELLOW}Failed to create virtual environment, python3-venv might be missing.${NC}"
    echo -e "${YELLOW}Attempting to install python3-venv via apt-get...${NC}"
    if ! command -v apt-get &> /dev/null; then
        echo -e "${RED}ERROR: apt-get not found. Please install python3-venv manually and rerun this script.${NC}"
        exit 1
    fi
    sudo apt-get update && sudo apt-get install -y python3-venv
    
    echo -e "${YELLOW}Retrying virtual environment creation...${NC}"
    if ! python3 -m venv "$INSTALL_DIR/venv"; then
        echo -e "${RED}ERROR: Failed to create virtual environment after installing python3-venv. Please check for errors.${NC}"
        exit 1
    fi
fi
echo -e "${GREEN}✅ Virtual environment created.${NC}"

# --- Step 5: Install Dependencies ---
echo -e "\n${YELLOW}Step 5: Installing required Python packages...${NC}"
if ! "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"; then
    echo -e "${RED}ERROR: Failed to install Python packages.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ All required packages installed successfully.${NC}"

# --- Step 6: Create Sample species_list.csv ---
echo -e "\n${YELLOW}Step 6: Checking for species_list.csv...${NC}"
if [ ! -f "$INSTALL_DIR/species_list.csv" ]; then
    echo -e "${YELLOW}File not found. Creating a sample species_list.csv...${NC}"
    cat > "$INSTALL_DIR/species_list.csv" << EOF
common_name,scientific_name
Australian Magpie,Gymnorhina tibicen
Laughing Kookaburra,Dacelo novaeguineae
Sulphur-crested Cockatoo,Cacatua galerita
EOF
    echo -e "${GREEN}✅ Sample file created. Please edit it with your local bird species for a better experience.${NC}"
else
    echo -e "${GREEN}✅ Existing species_list.csv found.${NC}"
fi

# --- Step 7: Build and Resize Image Cache ---
echo -e "\n${YELLOW}Step 7: Building and resizing the offline image cache...${NC}"
echo -e "${YELLOW}This may take a few minutes depending on your internet connection and species list size.${NC}"
if ! (cd "$INSTALL_DIR" && "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/cache_builder.py"); then
    echo -e "${RED}ERROR: Failed to build and resize the image cache. Please check for errors above.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Offline image cache is ready.${NC}"

# --- Step 8: Create Run Script ---
echo -e "\n${YELLOW}Step 8: Creating run.sh script...${NC}"
cat > "$INSTALL_DIR/run.sh" << EOF
#!/bin/bash
# This script activates the virtual environment and starts the Flask server.
echo "Starting the Bird Detection Display..."
export BIRDNET_DISPLAY_HOME="$INSTALL_DIR"
export BIRDNET_DISPLAY_STATIC_DIR="$DISPLAY_STATIC_DIR"
export BIRDNET_IMAGE_CACHE_DIR="$INSTALL_CACHE_DIR"
export BIRDNET_PI_HOME="\${BIRDNET_PI_HOME:-\$HOME/BirdNET-Pi}"
export BIRDNET_DB_PATH="\${BIRDNET_DB_PATH:-\$BIRDNET_PI_HOME/scripts/birds.db}"
export BIRDNET_AUDIO_DIR="\${BIRDNET_AUDIO_DIR:-\$HOME/BirdSongs/Extracted/By_Date}"
cd "$INSTALL_DIR"
source venv/bin/activate
python3 birdnet_display.py
EOF
chmod +x "$INSTALL_DIR/run.sh"
echo -e "${GREEN}✅ run.sh created and made executable.${NC}"

# --- Step 9: Optional Raspberry Pi Kiosk Setup ---
echo ""
read -p "Are you setting this up on a Raspberry Pi for a kiosk display? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "\n${YELLOW}--- Configuring Raspberry Pi Kiosk Mode ---${NC}"
    echo -e "${YELLOW}This section requires sudo permissions for system changes...${NC}"

    # 9.1: Install Kiosk dependencies
    echo -e "\n${YELLOW}Installing kiosk dependencies (chromium-browser, unclutter)...${NC}"
    sudo apt-get update
    sudo apt-get install -y chromium-browser unclutter

    # 9.2: Create and enable systemd service to run the app on boot
    echo -e "\n${YELLOW}Creating systemd service to auto-start the application...${NC}"
    SERVICE_FILE="/etc/systemd/system/bird-display.service"
    CURRENT_USER=$(whoami)
    
    # Write to a unique temporary file before moving it into /etc/systemd/system.
    SERVICE_TMP="$(mktemp)"
    cat > "$SERVICE_TMP" << EOF
[Unit]
Description=Bird Detection Display Flask App
After=network.target caddy.service birdnet-pi.service
Wants=caddy.service birdnet-pi.service

[Service]
User=$CURRENT_USER
Group=$(id -gn "$CURRENT_USER")
WorkingDirectory=$INSTALL_DIR
Environment=BIRDNET_DISPLAY_HOME=$INSTALL_DIR
ExecStart=$INSTALL_DIR/run.sh
Restart=always

[Install]
WantedBy=multi-user.target
EOF
    sudo mv "$SERVICE_TMP" "$SERVICE_FILE"

    echo -e "\n${YELLOW}Enabling and starting the service...${NC}"
    sudo systemctl daemon-reload
    sudo systemctl enable bird-display.service
    sudo systemctl start bird-display.service
    echo -e "${GREEN}✅ Systemd service created and enabled.${NC}"

    # 9.3: Configure desktop autostart using the more robust .desktop file method
    echo -e "\n${YELLOW}Configuring desktop autostart for kiosk mode...${NC}"
    
    # Create the launcher script with a delay
    LAUNCHER_SCRIPT="$INSTALL_DIR/kiosk_launcher.sh"
    cat > "$LAUNCHER_SCRIPT" << EOF
#!/bin/bash
# Add a delay to allow the desktop and network to fully initialize
sleep 15
# Launch Chromium
/usr/bin/chromium-browser --noerrdialogs --disable-infobars --kiosk http://localhost:5000
EOF
    chmod +x "$LAUNCHER_SCRIPT"
    echo -e "${GREEN}  - Created kiosk launcher script.${NC}"
    
    # Create the autostart directory
    AUTOSTART_DIR="$HOME/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"

    # Create the .desktop file
    DESKTOP_FILE="$AUTOSTART_DIR/bird-display-kiosk.desktop"
    cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=Bird Display Kiosk
Exec=$LAUNCHER_SCRIPT
Comment=Starts the bird display in kiosk mode.
EOF
    echo -e "${GREEN}✅ Desktop autostart configured using .desktop file.${NC}"

    echo -e "\n${GREEN}Kiosk setup is complete. A reboot is required for the changes to take effect.${NC}"
    REBOOT_REQUIRED=true
fi

# --- Step 10: Optional Advanced BirdNET-Go Networking ---
echo ""
echo -e "${YELLOW}Optional BirdNET-Go host networking is no longer configured by the main installer.${NC}"
echo -e "${YELLOW}For advanced opt-in setup, review and run:${NC}"
echo -e "  ${YELLOW}$INSTALL_DIR/scripts/configure_birdnet_go_networking.sh --dry-run${NC}"

# --- Reboot Prompt ---
if [ "$REBOOT_REQUIRED" = true ]; then
    echo ""
    read -p "Kiosk mode was configured. Reboot now to apply changes? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo reboot
    fi
fi

# --- Final Instructions ---
echo -e "\n${GREEN}--- 🎉 Setup Complete! ---${NC}"
echo -e "The application has been installed in ${YELLOW}$INSTALL_DIR${NC}"
echo -e "To start the application manually, simply run:"
echo -e "\n  ${YELLOW}$INSTALL_DIR/run.sh${NC}\n"
echo -e "Then, open a web browser on any device on your network to the server's IP address (e.g., http://192.168.1.123:5000)."
