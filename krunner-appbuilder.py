#!/usr/bin/env python3

# KRunner DBus Plugin for Host-Native App Builder
# Integrates standalone app building directly into KDE Plasma's KRunner.

import sys
import subprocess
import threading
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# Setup DBus Loop to listen for KDE events
DBusGMainLoop(set_as_default=True)

class AppBuilderRunner(dbus.service.Object):
    def __init__(self):
        # Register the unique DBus name for our plugin
        bus_name = dbus.service.BusName("org.kde.hostnative", bus=dbus.SessionBus())
        super().__init__(bus_name, "/runner")

    @dbus.service.method("org.kde.krunner1", in_signature="s", out_signature="a(sssida{sv})")
    def Match(self, query):
        """
        Triggered every time the user types something in KRunner.
        """
        query = query.strip().lower()
        
        # Only react if the user types "build " or "install "
        if not query.startswith("build ") and not query.startswith("install "):
            return []

        # Extract the application name
        app_name = query.split(" ", 1)[1].strip()
        if not app_name:
            return []

        # Construct the visual match for KRunner
        # Tuple format: (match_id, text, icon_name, match_type, relevance, properties)
        # Type 100 = ExactMatch
        match = (
            app_name,
            f"Build and install '{app_name}' as standalone app",
            "system-software-install",
            100,
            1.0,
            {}
        )
        return [match]

    @dbus.service.method("org.kde.krunner1", out_signature="a(sss)")
    def Actions(self):
        """
        Defines secondary actions (buttons next to the match). None needed here.
        """
        return []

    @dbus.service.method("org.kde.krunner1", in_signature="ss")
    def Run(self, match_id, action_id):
        """
        Triggered when the user presses Enter on our match.
        """
        app_name = match_id
        
        # Start the heavy building process in a background thread
        # This is CRITICAL so we don't freeze the KRunner UI!
        thread = threading.Thread(target=self.build_app_in_background, args=(app_name,))
        thread.start()

    def build_app_in_background(self, app_name):
        """
        Executes the toolbox build and sends desktop notifications.
        """
        # 1. Send initial notification
        subprocess.run([
            "notify-send", 
            "--icon=system-software-install", 
            "--app-name=App Builder",
            "Build Started", 
            f"Building {app_name} inside toolbox..."
        ])

        # IMPORTANT: Change this to the absolute path where your builder script lives
        builder_script = "/var/home/martin/development/appimage/appimage-builder.py"
        
        # Execute via toolbox
        cmd = ["toolbox", "run", "-c", "sysext-builder", builder_script, app_name, app_name]

        try:
            process = subprocess.run(cmd, capture_output=True, text=True)
            
            # 2. Send success or error notification
            if process.returncode == 0:
                subprocess.run([
                    "notify-send", 
                    "--icon=dialog-ok-apply", 
                    "--app-name=App Builder",
                    "Build Successful", 
                    f"{app_name.capitalize()} is now ready in your application menu!"
                ])
            else:
                subprocess.run([
                    "notify-send", 
                    "--icon=dialog-error", 
                    "--app-name=App Builder",
                    "Build Failed", 
                    f"Check terminal. Exit code: {process.returncode}"
                ])
        except Exception as e:
            subprocess.run(["notify-send", "--icon=dialog-error", "Build Error", str(e)])

if __name__ == "__main__":
    runner = AppBuilderRunner()
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
