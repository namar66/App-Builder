# Host-Native App Builder (for Atomic Fedora) (proof of concept)

A modern graphical package manager and build tool that creates blazing-fast, standalone `.app` executables using **EROFS** and **Bubblewrap**. 

Designed specifically for immutable/atomic Linux distributions like **Fedora Kinoite** and **Silverblue**, it allows you to install GUI applications without overlaying your base system (`rpm-ostree`) or relying on legacy FUSE 2/SquashFS implementations used by traditional AppImages.

## 🚀 Why this exists?

Traditional AppImages are great, but they often struggle on modern atomic systems:
1. They rely on outdated SquashFS and require `libfuse2` (which is often removed in modern distros like Fedora 43+).
2. They bundle redundant libraries that your host system already has, wasting space.
3. They don't easily integrate with native Wayland/GPU environments without tweaking.

**Host-Native App Builder solves this by:**
* Using **EROFS** (Enhanced Read-Only File System) for lightning-fast, highly compressed payloads.
* Using **Bubblewrap** (`bwrap`) to run the app in a lightweight namespace sandbox.
* Being **Host-Aware**: It queries your host OS and only bundles dependencies that your system *actually lacks*.
* Integrating seamlessly: Automatically extracts native icons and `.desktop` files directly into your KDE/GNOME application menu.

## ✨ Key Features

* **Discover-like GUI:** A clean PyQt6 interface to search, browse groups, and queue applications from the Fedora DNF repositories.
* **Toolbox Backend:** The heavy lifting (downloading RPMs, resolving dependencies, compiling the C wrapper) happens safely inside a Toolbox container, keeping your host completely clean.
* **Zero-Copy Native Execution:** Uses `erofsfuse` to read the payload directly from the executable binary without copying data to RAM.
* **Full GPU & Wayland Support:** The sandbox binds `/dev`, `/tmp`, and `/run`, ensuring hardware acceleration and native display server compatibility.
* **One-Click Integration:** Automatically places the generated `.app` in `~/.local/bin/` and creates a `.desktop` shortcut with the original high-res icon.

## 🛠️ Architecture

The project consists of two main components:
1. `app-manager-gui.py`: The PyQt6 frontend running on your host system. It handles user interaction and queries local RPM databases.
2. `appimage-builder.py`: The CLI builder running inside a Toolbox. It downloads packages, creates the EROFS image, compiles a tiny C-based runtime wrapper, and stitches everything into a single `.app` executable.

## 📦 Prerequisites

**On your Host System (Fedora Kinoite/Silverblue):**
```bash
rpm-ostree install python3-pyqt6 erofs-utils
```
or use sysext for install deps

# Reboot to apply changes
Inside your Toolbox (named sysext-builder by default):

```Bash
toolbox create -c sysext-builder
toolbox enter -c sysext-builder
sudo dnf install erofs-utils gcc rpm cpio flatpak-spawn
```
🚀 Usage
Clone this repository.

Make both scripts executable: chmod +x app-manager-gui.py appimage-builder.py.

Run the GUI on your host:

Bash
./app-manager-gui.py
Search for an app (e.g., krusader), right-click to add to the queue, and hit Build App.

Once finished, launch your new app directly from your desktop environment's application launcher!

📝 License
[GPLv2]
