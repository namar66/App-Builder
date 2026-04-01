# Atomic App Builder (AppImage Creator Pro) (still under construstion)

A powerful, native, and highly optimized application bundler and sandboxing tool designed specifically for immutable Linux distributions like **Fedora Kinoite / Silverblue**. 

It takes standard RPM packages, resolves dependencies, and compiles them into a single, standalone `.app` executable. It uses **EROFS** for ultra-fast compressed storage and **Bubblewrap (`bwrap`)** combined with **FUSE** for strict, Flatpak-style sandboxing.

## 🚀 Features

* **True Standalone Executables:** No extraction to disk required. The app mounts its own EROFS payload into memory via FUSE upon execution.
* **Advanced Sandboxing:** Built-in `bwrap` integration. Apps run in an isolated namespace with a Private Home directory (`~/.var/app/<name>`) to keep your host system clean.
* **Host-Native & Portable Modes:** * *Host-Native:* Drastically reduces file size by utilizing base libraries already present on your host OS (via `rpm-ostree` dependency analysis).
  * *Portable:* Bundles all dependencies (including 32-bit libraries for Steam/Wine) for use across different systems.
* **Flathub Metadata Integration:** Dynamically fetches sandbox permissions (Wayland, X11, PulseAudio, DRI, filesystem binds) directly from Flathub profiles.
* **High-Performance C Wrapper:** The entrypoint is a custom-compiled, highly optimized C binary, ensuring zero overhead compared to Python-based launchers.
* **Auto-Updater:** Includes a background daemon to query DNF repositories and automatically rebuild outdated `.app` containers.

## 🛠️ Architecture

The suite consists of three main components:
1. `app-builder-gui.py`: A PyQt6 GUI for managing installed apps, queues, and configuring granular sandbox permissions.
2. `appimage-builder.py`: The core CLI engine that runs inside a `toolbox` container. It downloads RPMs, builds the EROFS image, and compiles the C wrapper.
3. `app-auto-updater.py`: A background script that safely checks for updates and triggers rebuilds.

## 📦 Prerequisites

Since this is designed for Atomic Fedora desktops, the host requirements are minimal. The heavy lifting is done inside a `toolbox` container.

**Host Requirements:**
```bash
# Ensure you have EROFS FUSE tools and bubblewrap installed on your host
rpm-ostree install erofs-utils
```
or use sysext
## ⚙️ Installation & Setup

1. Clone this repository and move the scripts to your local bin directory:
   ```bash
   git clone [https://github.com/yourusername/atomic-app-builder.git](https://github.com/yourusername/atomic-app-builder.git)
   cd atomic-app-builder
   chmod +x *.py
   mkdir -p ~/.local/bin
   cp *.py ~/.local/bin/
   ```

2. The GUI will automatically create and initialize the required `sysext-builder` toolbox container on its first run.

## 🎮 Usage

### Graphical Interface (Recommended)
Simply launch the GUI:
```bash
~/.local/bin/app-builder-gui.py
```
* Browse available packages via DNF.
* Add them to the Transaction Queue.
* Select **Host-Native** or **Portable** mode.
* Click "Apply Transaction" to generate your `.app` files.
* Go to the "Sandbox Permissions" tab to load Flatpak metadata or manually toggle GPU, Wayland, and folder access.

### CLI Mode
You can build applications directly from the terminal. The syntax is:
```bash
appimage-builder.py <App-Name> <Portable|Host-Native> [Optional-Extra-Packages...]
```

**Example (Building Steam with 32-bit dependencies):**
```bash
~/.local/bin/appimage-builder.py steam Portable steam steam-devices
```

**Example (Building a slim Krusader relying on host KDE libraries):**
```bash
~/.local/bin/appimage-builder.py krusader Host-Native krusader kompare
```

## 🛡️ Sandbox Management & Pressure Vessel

The generated C wrapper is intelligent enough to handle complex nested sandboxes, such as Steam's **Pressure Vessel**. It dynamically manages:
* Hardware acceleration (`/dev/dri`)
* X11 and Wayland sockets
* PulseAudio routing
* Fake `ld.so.cache` generation for cross-distribution compatibility inside the container.

To completely disable the sandbox for a specific app (giving it full host access while still mounting the EROFS bundle), add the following to `~/.var/app/<app-name>/metadata`:
```ini
[Sysext]
nosandbox=true
```
