# Atomic AppImage Creator Pro

An unapologetically advanced, hyper-secure, and self-healing application packager and sandbox manager built specifically for Fedora Atomic desktops (Kinoite/Silverblue). 

Tired of bloated Flatpaks, dependency hell, and containers that either have too many permissions or break when you look at them funny? This tool builds ultra-slim, natively integrated `.app` executables powered by High-Performance EROFS compression and Bubblewrap.

## 🚀 The Magic Inside

* **Smart Host-Native Routing:** Stop downloading half an operating system just to run a calculator. By interrogating `rpm-ostree`, this tool calculates exactly which shared libraries your host system already provides and bundles *only* the truly missing dependencies. Result? Apps that boot instantly and weigh megabytes, not gigabytes.
* **32-Bit Multilib Sorcery:** Capable of running massive 32-bit legacy applications (like Steam and Proton) natively on a pure 64-bit atomic host. We achieve this via on-the-fly virtual `/lib` injection to perfectly bypass UsrMerge constraints without touching your host system.
* **Anti-Flatkill Security Architecture:** We took the sandbox escape vectors (like compromised apps rewriting your `~/.bashrc` or `~/.ssh/` keys) and nuked them. Critical host shell configurations are hard-locked via `/dev/null` and strict `ro-binds` at the C-wrapper level. Even if you give an app full RW access to your home directory, it cannot infect your host shell.
* **Self-Healing Build Environment:** Doesn't clutter your host. Automatically spins up and manages an isolated Podman `toolbox` for downloading and building packages. It seamlessly syncs your host's repositories and GPG keys (automatically handling Fedora's strict crypto-policies) to ensure every downloaded RPM is cryptographically verified.
* **SELinux & EROFS Integration:** Bundles everything into a single, high-speed Read-Only File System (EROFS) executable while pulling native SELinux file contexts directly from the host to prevent silent AVC denial headaches.

## 🛠️ Prerequisites

You are expected to be running an atomic Linux distribution (tested heavily on Fedora Kinoite). 
You will need:
* `python3` and `python3-pyqt6`
* `podman` and `toolbox`
* `bwrap` (Bubblewrap)
* `fuse3` and `erofs-utils` (specifically `erofsfuse` and `mkfs.erofs`)

## 📦 Installation & Usage

1. Place `appimage-builder.py` and `app-builder-gui.py` into `~/.local/bin/`.
2. Make them executable:
   ```bash
   chmod +x ~/.local/bin/appimage-builder.py
   chmod +x ~/.local/bin/app-builder-gui.py
   ```
3. Launch the graphical interface:
   ```bash
   python3 ~/.local/bin/app-builder-gui.py
   ```
4. On the first launch, the GUI will silently construct the `sysext-builder` container in the background. Once initialized, search for your target application, add it to the Transaction Queue, and build!

## 🛡️ Dynamic Sandbox Control

Every bundled application gets a beautifully isolated private home directory (`~/.var/app/<app-name>`). 
Need to give an app access to your real files or hardware? Select the app in the **Installed** tab of the GUI and edit its permissions on the fly:

* **Bind Mounts:** Add `/var/home/username` to let a file manager see your files. Add `/run/media` for external drives.
* **Masks:** Want to hide a specific host path from the application? Add it here, and the sandbox will overlay it with an empty `tmpfs`.
* *Note: Changes to the sandbox apply immediately on the next application launch. No rebuild required.*
