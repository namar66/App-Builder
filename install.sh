#!/usr/bin/env bash

# Host-Native App Builder Installation Script
# This script deploys the python executables and registers the KDE KRunner plugin.

set -e

echo "Starting installation of Host-Native App Builder..."

# Define target directories in the user's home folder
BIN_DIR="$HOME/.local/bin"
DBUS_DIR="$HOME/.local/share/dbus-1/services"
KRUNNER_DIR="$HOME/.local/share/krunner/dbusplugins"

# Create necessary directories if they don't exist
mkdir -p "$BIN_DIR"
mkdir -p "$DBUS_DIR"
mkdir -p "$KRUNNER_DIR"

# Copy python scripts to the local binary path
echo "Copying executables to $BIN_DIR..."
cp appimage-builder.py "$BIN_DIR/"
cp sysext-creator-new-gui.py "$BIN_DIR/"
cp krunner-appbuilder.py "$BIN_DIR/"

# Ensure scripts are executable
chmod +x "$BIN_DIR"/appimage-builder.py
chmod +x "$BIN_DIR"/sysext-creator-new-gui.py
chmod +x "$BIN_DIR"/krunner-appbuilder.py

# Generate D-Bus service file dynamically to match the user's home directory
echo "Registering D-Bus service..."
cat > "$DBUS_DIR/org.kde.hostnative.service" <<EOF
[D-BUS Service]
Name=org.kde.hostnative
Exec=$BIN_DIR/krunner-appbuilder.py
EOF

# Generate KRunner desktop integration file
echo "Registering KRunner plugin..."
cat > "$KRUNNER_DIR/plasma-runner-hostnative.desktop" <<EOF
[Desktop Entry]
Name=Host-Native App Installer
Comment=Build standalone apps via toolbox
X-Plasma-API=DBus
X-Plasma-DBusRunner-Service=org.kde.hostnative
X-Plasma-DBusRunner-Path=/runner
X-Plasma-Request-Actions-Once=true
X-Plasma-Runner-Min-Letter-Count=3
X-Plasma-Runner-Syntaxes=install :q:,build :q:
X-Plasma-Runner-Syntax-Descriptions=Builds and installs an application
Type=Service
Icon=system-software-install
EOF

# Restart KRunner to apply the new plugin
echo "Restarting KRunner..."
kquitapp6 krunner 2>/dev/null || true
kstart krunner >/dev/null 2>&1 &

echo ""
echo "================================================="
echo "Installation Complete!"
echo "Ensure that $BIN_DIR is in your system PATH."
echo "Press Alt+Space and type 'build <app_name>' to test."
echo "================================================="
