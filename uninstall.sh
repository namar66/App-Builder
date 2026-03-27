#!/usr/bin/env bash

# Host-Native App Builder Uninstallation Script

echo "Removing Host-Native App Builder components..."

BIN_DIR="$HOME/.local/bin"
DBUS_DIR="$HOME/.local/share/dbus-1/services"
KRUNNER_DIR="$HOME/.local/share/krunner/dbusplugins"

# Remove executables
rm -f "$BIN_DIR/appimage-builder.py"
rm -f "$BIN_DIR/sysext-creator-new-gui.py"
rm -f "$BIN_DIR/krunner-appbuilder.py"

# Remove system integration files
rm -f "$DBUS_DIR/org.kde.hostnative.service"
rm -f "$KRUNNER_DIR/plasma-runner-hostnative.desktop"

# Restart KRunner to clear the plugin cache
echo "Restarting KRunner..."
kquitapp6 krunner 2>/dev/null || true
kstart krunner >/dev/null 2>&1 &

echo "Uninstallation complete."
