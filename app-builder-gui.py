#!/usr/bin/python3

# Advanced Application Manager GUI
# Handles backend operations securely and efficiently.
# Features: Standalone .app management, permissions editing, self-healing init, robust encoding, and Smart Host-Native routing.

import sys
import os
import subprocess
import logging
import re
import shutil
import json
import configparser
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QSplitter, QListWidget, QTableView,
                             QLineEdit, QTabWidget, QTextEdit, QLabel, QPushButton,
                             QHeaderView, QMenu, QMessageBox, QAbstractItemView,
                             QComboBox, QCheckBox, QInputDialog, QGridLayout, QGroupBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QAction

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def calculate_host_dependencies(packages):
    """Calculates missing dependencies using rpm-ostree dry-run against the host."""
    if not packages: return []
    logging.info("Analyzing host system and calculating missing dependencies...")

    try:
        arch_res = subprocess.run(["uname", "-m"], capture_output=True, text=True, check=True)
        host_arch = arch_res.stdout.strip()
    except:
        host_arch = "x86_64"

    allowed_archs = ["noarch", host_arch]
    if host_arch == "x86_64": allowed_archs.append("i686")

    cmd = ["env", "LANG=C", "rpm-ostree", "install", "--dry-run"] + packages
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")

        if res.returncode != 0:
            err_msg = res.stderr.lower()
            if "is already provided" in err_msg or "already installed" in err_msg:
                return "ALREADY_INSTALLED"
            return "ERROR"

        added_pkgs = []
        parsing_added = False

        for line in res.stdout.splitlines():
            if line.startswith("Installing ") or line.startswith("Upgrading ") or line.startswith("Added:") or line.startswith("Upgraded:"):
                parsing_added = True
                continue
            elif line.startswith("Removing ") or line.startswith("Removed:") or line.startswith("Transaction:"):
                parsing_added = False
                continue

            if parsing_added and (line.startswith("  ") or line.startswith(" ")):
                raw_pkg = line.strip().split()[0]
                parts = raw_pkg.rsplit('.', 1)
                if len(parts) == 2 and parts[1] in allowed_archs:
                    nvr = parts[0]
                    nvr_parts = nvr.rsplit('-', 2)
                    if len(nvr_parts) >= 3:
                        pkg_name = nvr_parts[0]
                        pkg_arch = parts[1]
                        formatted = f"{pkg_name}.{pkg_arch}"
                        if formatted not in added_pkgs:
                            added_pkgs.append(formatted)
        return added_pkgs
    except Exception as e:
        logging.error(f"Critical failure during host dependency calculation: {e}")
        return "ERROR"

class ContainerInitWorker(QThread):
    log_msg = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def run(self):
        container_name = "sysext-builder"
        self.log_msg.emit(f"Checking if toolbox container '{container_name}' exists...")
        res = subprocess.run(["podman", "container", "exists", container_name], capture_output=True, text=True, errors="replace")

        if res.returncode != 0:
            self.log_msg.emit(f"Container missing. Creating '{container_name}' (this may take a minute)...")
            create_res = subprocess.run(["toolbox", "create", "-y", "-c", container_name], capture_output=True, text=True, errors="replace")
            if create_res.returncode != 0:
                self.finished.emit(False, f"Failed to create toolbox: {create_res.stderr}")
                return

        self.log_msg.emit("Syncing host repositories and installing dependencies...")
        setup_script = (
            "cp -fa /run/host/etc/yum.repos.d/*.repo /etc/yum.repos.d/ 2>/dev/null || true; "
            "cp -fa /run/host/etc/pki/rpm-gpg/* /etc/pki/rpm-gpg/ 2>/dev/null || true; "
            "update-crypto-policies --set LEGACY 2>/dev/null || true; "
            "rpm --import /etc/pki/rpm-gpg/* 2>/dev/null || true; "
            "LANG=C dnf install -y gcc erofs-utils rpm cpio dnf-plugins-core"
        )
        setup_cmd = ["toolbox", "run", "-c", container_name, "sudo", "bash", "-c", setup_script]
        install_res = subprocess.run(setup_cmd, capture_output=True, text=True, errors="replace")

        if install_res.returncode != 0:
            self.finished.emit(False, f"Failed to setup container environment: {install_res.stderr}")
            return

        self.log_msg.emit("Container initialization complete. Ready to build.")
        self.finished.emit(True, "")

class DnfAsyncWorker(QThread):
    packages_loaded = pyqtSignal(list)
    groups_loaded = pyqtSignal(list)
    group_details_loaded = pyqtSignal(list)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, task="available", group_name=None):
        super().__init__()
        self.task = task
        self.group_name = group_name

    def load_groups(self):
        cmd = ["toolbox", "run", "-c", "sysext-builder", "env", "LANG=C", "dnf", "group", "list", "--hidden"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            if res.returncode != 0:
                self.error.emit(f"DNF Group List Error: {res.stderr.strip()}")
                return

            groups = []
            for line in res.stdout.splitlines():
                clean_line = line.strip()
                if not clean_line or clean_line.startswith(("ID", "Available", "Installed", "Hidden", "Environment", "Last metadata", "Aktualizace", "Repozitáře")):
                    continue
                parts = re.split(r'\s{2,}', clean_line)
                if len(parts) >= 3: groups.append([parts[0], "Group", parts[1], parts[2]])
                elif len(parts) == 2: groups.append([parts[0], "Group", parts[1], "Unknown"])

            self.groups_loaded.emit(groups)
        except Exception as e:
            self.error.emit(f"Failed to load groups: {e}")

    def load_group_details(self):
        cmd = ["toolbox", "run", "-c", "sysext-builder", "env", "LANG=C", "dnf", "group", "info", "--quiet", self.group_name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            packages, parsing_target = [], False

            for line in res.stdout.splitlines():
                if not line.strip(): continue
                if ":" in line:
                    left_clean, right_clean = line.split(":", 1)[0].strip(), line.split(":", 1)[1].strip()
                    if left_clean in ["Mandatory packages", "Default packages"]:
                        parsing_target = True
                        if right_clean: packages.append(right_clean)
                    elif left_clean == "":
                        if parsing_target and right_clean: packages.append(right_clean)
                    else:
                        parsing_target = False

            self.group_details_loaded.emit(packages)
        except Exception as e:
            self.error.emit(f"Failed to load group details: {e}")

    def load_available_packages(self):
        cmd = ["toolbox", "run", "-c", "sysext-builder", "env", "LANG=C", "dnf", "repoquery", "--quiet", "--queryformat", "%{name}|%{version}-%{release}|%{repoid}\n"]
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            batch = []
            for line in process.stdout:
                clean_line = line.strip()
                if not clean_line or "metadata" in clean_line.lower(): continue
                parts = clean_line.split("|")
                if len(parts) == 3: batch.append([parts[0], parts[1], parts[2], "Available"])

                if len(batch) >= 500:
                    self.packages_loaded.emit(batch)
                    batch = []
            if batch: self.packages_loaded.emit(batch)
            process.wait()
            if process.returncode != 0: self.error.emit("DNF query execution failed.")
        except Exception as e:
            self.error.emit(f"Error executing DNF process: {e}")

    def load_installed_and_updates(self, check_updates=False):
        bin_dir = os.path.expanduser("~/.local/bin")
        if not os.path.exists(bin_dir):
            self.packages_loaded.emit([])
            return

        safe_env = os.environ.copy()
        safe_env.pop("DISPLAY", None)
        safe_env.pop("WAYLAND_DISPLAY", None)

        installed_apps = {}
        for f in os.listdir(bin_dir):
            if f.endswith(".app"):
                app_name = f[:-4]
                try:
                    res_ver = subprocess.run([os.path.join(bin_dir, f), "--app-version"], capture_output=True, text=True, timeout=2, errors="replace", env=safe_env)
                    res_mod = subprocess.run([os.path.join(bin_dir, f), "--app-mode"], capture_output=True, text=True, timeout=2, errors="replace", env=safe_env)

                    ver_out = res_ver.stdout.strip()
                    mod_out = res_mod.stdout.strip() if res_mod.returncode == 0 else "Unknown"

                    if res_ver.returncode == 0 and 0 < len(ver_out) < 50:
                        installed_apps[app_name] = {"ver": ver_out, "mode": mod_out}
                    else:
                        installed_apps[app_name] = {"ver": "unknown", "mode": "Unknown"}
                except Exception:
                    installed_apps[app_name] = {"ver": "error", "mode": "Unknown"}

        if not installed_apps:
            self.packages_loaded.emit([])
            return

        if not check_updates:
            batch = [[name, f"{data['ver']} [{data['mode']}]", "~/.local/bin", "Installed"] for name, data in installed_apps.items()]
            self.packages_loaded.emit(batch)
            return

        cmd = ["toolbox", "run", "-c", "sysext-builder", "env", "LANG=C", "dnf", "repoquery", "--latest-limit", "1", "--quiet", "--queryformat", "%{name}|%{version}-%{release}\n"] + list(installed_apps.keys())
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            latest_versions = {line.strip().split("|")[0]: line.strip().split("|")[1] for line in res.stdout.splitlines() if "|" in line}

            updates_batch = []
            for name, data in installed_apps.items():
                inst_ver = data["ver"]
                lat_ver = latest_versions.get(name, inst_ver)
                if inst_ver != lat_ver and lat_ver != "unknown" and inst_ver != "unknown":
                    updates_batch.append([name, inst_ver, lat_ver, "Update Available"])

            self.packages_loaded.emit(updates_batch)
        except Exception as e:
            self.error.emit(f"Failed to check updates: {e}")

    def run(self):
        if self.task == "available": self.load_available_packages()
        elif self.task == "groups": self.load_groups()
        elif self.task == "group_details": self.load_group_details()
        elif self.task == "installed": self.load_installed_and_updates(check_updates=False)
        elif self.task == "updates": self.load_installed_and_updates(check_updates=True)
        self.finished.emit()

class BuildAsyncWorker(QThread):
    log_msg = pyqtSignal(str)
    finished_all = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, queue, mode, merge=False):
        super().__init__()
        self.queue = queue
        self.mode = mode
        self.merge = merge

    def run(self):
        builder_script = os.path.expanduser("~/.local/bin/appimage-builder.py")
        if not os.path.exists(builder_script):
            self.error.emit(f"Builder script not found at {builder_script}. Ensure it is placed in ~/.local/bin/")
            return

        # Handle Merged Queue
        if self.merge and self.queue:
            main_app = self.queue[0]
            self.log_msg.emit(f"<br><b style='color: #2e8b57;'>--- Preparing MERGED build for {main_app} in {self.mode} mode ---</b>")
            self.log_msg.emit(f"<i>Packages included: {', '.join(self.queue)}</i>")

            final_packages = self.queue

            if self.mode == "Host-Native":
                self.log_msg.emit("<i>Analyzing host dependencies via rpm-ostree...</i>")
                deps = calculate_host_dependencies(final_packages)

                if deps == "ALREADY_INSTALLED":
                    self.log_msg.emit(f"<b style='color: #d18c00;'>Skipping:</b> All requested packages are already provided by the host system.")
                    self.finished_all.emit()
                    return
                elif deps == "ERROR":
                    self.log_msg.emit(f"<span style='color:red;'>Error:</span> Host dependency calculation failed. Falling back to simple bundle.")
                else:
                    final_packages = deps
                    self.log_msg.emit(f"<i>Found {len(final_packages)} required packages to bundle.</i>")

            self.log_msg.emit(f"<i>Starting builder inside toolbox...</i>")
            cmd = ["toolbox", "run", "-c", "sysext-builder", "env", "LANG=C", builder_script, main_app, self.mode] + final_packages

            try:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
                for line in process.stdout: self.log_msg.emit(line.strip())
                process.wait()

                if process.returncode == 0:
                    self.log_msg.emit(f"<b style='color: #005cc5;'>--- Successfully finished merged app: {main_app} ---</b><br>")
                else:
                    self.log_msg.emit(f"<b style='color: red;'>--- Build failed for {main_app} (Exit code: {process.returncode}) ---</b><br>")
            except Exception as e:
                self.error.emit(f"Execution error for {main_app}: {str(e)}")

        else:
            # Original loop for individual apps
            for app_name in self.queue:
                self.log_msg.emit(f"<br><b style='color: #2e8b57;'>--- Preparing {app_name} in {self.mode} mode ---</b>")

                final_packages = [app_name]

                if self.mode == "Host-Native":
                    self.log_msg.emit("<i>Analyzing host dependencies via rpm-ostree...</i>")
                    deps = calculate_host_dependencies([app_name])

                    if deps == "ALREADY_INSTALLED":
                        self.log_msg.emit(f"<b style='color: #d18c00;'>Skipping:</b> Application '{app_name}' is already provided by the host system.")
                        continue
                    elif deps == "ERROR":
                        self.log_msg.emit(f"<span style='color:red;'>Error:</span> Host dependency calculation failed. Falling back to simple bundle.")
                    else:
                        final_packages = deps
                        self.log_msg.emit(f"<i>Found {len(final_packages)} required packages to bundle.</i>")

                self.log_msg.emit(f"<i>Starting builder inside toolbox...</i>")
                cmd = ["toolbox", "run", "-c", "sysext-builder", "env", "LANG=C", builder_script, app_name, self.mode] + final_packages

                try:
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
                    for line in process.stdout: self.log_msg.emit(line.strip())
                    process.wait()

                    if process.returncode == 0:
                        self.log_msg.emit(f"<b style='color: #005cc5;'>--- Successfully finished {app_name} ---</b><br>")
                    else:
                        self.log_msg.emit(f"<b style='color: red;'>--- Build failed for {app_name} (Exit code: {process.returncode}) ---</b><br>")
                except Exception as e:
                    self.error.emit(f"Execution error for {app_name}: {str(e)}")

        self.finished_all.emit()

class FlatpakProfileWorker(QThread):
    finished_success = pyqtSignal(str, str)
    finished_error = pyqtSignal(str)

    def __init__(self, app_id):
        super().__init__()
        self.app_id = app_id

    def run(self):
        try:
            # 15s timeout so we don't hang forever on bad networks
            res = subprocess.run(
                ["flatpak", "remote-info", "--show-metadata", "flathub", self.app_id],
                capture_output=True, text=True, timeout=15
            )
            if res.returncode == 0 and res.stdout.strip():
                self.finished_success.emit(self.app_id, res.stdout.strip())
            else:
                self.finished_error.emit(f"Failed to fetch metadata for {self.app_id}.\nMake sure the ID is correct and Flathub is reachable.")
        except subprocess.TimeoutExpired:
            self.finished_error.emit(f"Connection timed out while fetching metadata for {self.app_id}.")
        except Exception as e:
            self.finished_error.emit(f"Unexpected error occurred: {str(e)}")

class SysextAdvancedGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AppImage Creator Pro")
        self.resize(1100, 750)
        self.transaction_queue = []
        self.selected_installed_app = None
        self.setup_ui()

        self.category_list.setEnabled(False)
        self.start_initialization()

    def start_initialization(self):
        self.status_label.setText("⚙️ Initializing and verifying build environment...")
        self.tab_info.setHtml("<h3>System Initialization</h3><p>Checking container health and syncing repositories from host...</p>")
        self.init_worker = ContainerInitWorker()
        self.init_worker.log_msg.connect(self.append_build_log)
        self.init_worker.finished.connect(self.on_initialization_finished)
        self.init_worker.start()

    def on_initialization_finished(self, success, error_msg):
        if success:
            self.status_label.setText("✅ Environment ready. Select a category.")
            self.category_list.setEnabled(True)
        else:
            self.status_label.setText("❌ Initialization Failed.")
            QMessageBox.critical(self, "Initialization Error", f"Failed to setup the build container:\n{error_msg}")

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(main_splitter)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.category_list = QListWidget()
        self.category_list.addItems(["📦 Available (DNF)", "✅ Installed", "🔄 Updates", "📁 Package Groups", "🛒 Transaction Queue (0)"])
        self.category_list.currentRowChanged.connect(self.on_category_changed)

        left_layout.addWidget(QLabel("<b>Categories</b>"))
        left_layout.addWidget(self.category_list)

        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["Host-Native (Slim bundle, uses host libs)", "Portable (Standalone, includes dependencies)"])
        left_layout.addWidget(QLabel("<b>Build Mode</b>"))
        left_layout.addWidget(self.combo_mode)

        self.chk_merge_queue = QCheckBox("🔗 Merge Queue into Single App")
        self.chk_merge_queue.setToolTip("Combines all packages in the queue into a single .app based on the first item's name.")
        left_layout.addWidget(self.chk_merge_queue)

        self.btn_apply = QPushButton("Apply Transaction")
        self.btn_apply.setEnabled(False)
        self.btn_apply.setStyleSheet("background-color: #2e8b57; color: white; font-weight: bold; padding: 10px;")
        self.btn_apply.clicked.connect(self.apply_transaction)
        left_layout.addWidget(self.btn_apply)

        right_splitter = QSplitter(Qt.Orientation.Vertical)

        top_right_panel = QWidget()
        top_right_layout = QVBoxLayout(top_right_panel)
        top_right_layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Initializing...")
        top_right_layout.addWidget(self.status_label)

        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search packages...")
        top_right_layout.addWidget(self.search_bar)

        self.package_table = QTableView()
        self.package_model = QStandardItemModel(0, 4)
        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setSourceModel(self.package_model)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy_model.setFilterKeyColumn(0)

        self.package_table.setModel(self.proxy_model)
        self.package_table.setSortingEnabled(True)
        self.package_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.search_bar.textChanged.connect(self.proxy_model.setFilterFixedString)
        self.package_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.package_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.package_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.package_table.customContextMenuRequested.connect(self.show_context_menu)
        self.package_table.selectionModel().selectionChanged.connect(self.on_table_selection)

        top_right_layout.addWidget(self.package_table)

        self.details_tabs = QTabWidget()
        self.tab_info = QTextEdit()
        self.tab_info.setReadOnly(True)
        self.details_tabs.addTab(self.tab_info, "Information")

        # --- REBUILT Sandbox Permissions Tab ---
        self.tab_permissions = QWidget()
        perm_layout = QVBoxLayout(self.tab_permissions)

        # Load Flathub Profile Button
        self.btn_load_profile = QPushButton("🌐 Load Profile from Flathub")
        self.btn_load_profile.setStyleSheet("font-weight: bold; color: #005cc5;")
        self.btn_load_profile.clicked.connect(self.load_profile)
        perm_layout.addWidget(self.btn_load_profile)

        # Checkboxes for basic toggles
        perms_group = QGroupBox("Core Toggles")
        grid = QGridLayout(perms_group)
        self.chk_network = QCheckBox("Network")
        self.chk_wayland = QCheckBox("Wayland")
        self.chk_x11 = QCheckBox("X11")
        self.chk_pulseaudio = QCheckBox("PulseAudio")
        self.chk_dri = QCheckBox("DRI (OpenGL)")
        self.chk_ipc = QCheckBox("IPC")
        self.chk_dbus_user = QCheckBox("DBus User")
        self.chk_nosandbox = QCheckBox("No Sandbox")

        grid.addWidget(self.chk_network, 0, 0)
        grid.addWidget(self.chk_wayland, 0, 1)
        grid.addWidget(self.chk_x11, 0, 2)
        grid.addWidget(self.chk_pulseaudio, 0, 3)
        grid.addWidget(self.chk_dri, 1, 0)
        grid.addWidget(self.chk_ipc, 1, 1)
        grid.addWidget(self.chk_dbus_user, 1, 2)
        grid.addWidget(self.chk_nosandbox, 1, 3)
        perm_layout.addWidget(perms_group)

        # D-Bus configuration
        perm_layout.addWidget(QLabel("<b>D-Bus (Talk/Own):</b> One per line (e.g., --talk=org.freedesktop.Notifications)"))
        self.edit_dbus = QTextEdit()
        self.edit_dbus.setMaximumHeight(60)
        perm_layout.addWidget(self.edit_dbus)

        # Binds & Masks
        perm_layout.addWidget(QLabel("<b>Bind Mounts (Allowed Paths):</b>"))
        self.edit_binds = QTextEdit()
        self.edit_binds.setMaximumHeight(60)
        perm_layout.addWidget(self.edit_binds)

        perm_layout.addWidget(QLabel("<b>Masks (Blocked Paths):</b>"))
        self.edit_masks = QTextEdit()
        self.edit_masks.setMaximumHeight(60)
        perm_layout.addWidget(self.edit_masks)

        self.btn_save_perms = QPushButton("💾 Save Sandbox Permissions")
        self.btn_save_perms.clicked.connect(self.save_permissions)
        perm_layout.addWidget(self.btn_save_perms)

        self.details_tabs.addTab(self.tab_permissions, "Sandbox Permissions")

        right_splitter.addWidget(top_right_panel)
        right_splitter.addWidget(self.details_tabs)
        right_splitter.setSizes([500, 350])

        main_splitter.addWidget(left_panel)
        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([250, 850])

    def get_profiles_db(self):
        """Dummy method to return local offline database of parsed profiles."""
        db_path = os.path.expanduser("~/.local/share/app-builder-profiles.json")
        if os.path.exists(db_path):
            try:
                with open(db_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Failed to load local DB: {e}")
        return {}

    def load_profile(self):
        if not self.selected_installed_app:
            QMessageBox.information(self, "Notice", "Please select an installed app to apply the profile to.")
            return

        app_id, ok = QInputDialog.getText(self, "Load Profile", "Enter Flathub ID (e.g. com.spotify.Client):")
        if not ok or not app_id.strip(): return
        app_id = app_id.strip()

        # Reset UI to paranoid state
        self.chk_network.setChecked(False)
        self.chk_wayland.setChecked(False)
        self.chk_x11.setChecked(False)
        self.chk_pulseaudio.setChecked(False)
        self.chk_dri.setChecked(False)
        self.chk_ipc.setChecked(True)
        self.chk_dbus_user.setChecked(False)
        self.chk_nosandbox.setChecked(False)
        self.edit_dbus.clear()
        self.edit_binds.clear()

        db = self.get_profiles_db()
        dbus_texts = []
        binds_texts = []

        if app_id in db:
            args = db[app_id]
            for arg in args:
                if arg == "--share=network": self.chk_network.setChecked(True)
                elif arg == "--socket=wayland": self.chk_wayland.setChecked(True)
                elif arg in ["--socket=x11", "--socket=fallback-x11"]: self.chk_x11.setChecked(True)
                elif arg == "--socket=pulseaudio": self.chk_pulseaudio.setChecked(True)
                elif arg == "--device=dri": self.chk_dri.setChecked(True)
                elif arg.startswith("--talk-name="):
                    self.chk_dbus_user.setChecked(True)
                    dbus_texts.append(f"--talk={arg.split('=')[1]}")
                elif arg.startswith("--own-name="):
                    self.chk_dbus_user.setChecked(True)
                    dbus_texts.append(f"--own={arg.split('=')[1]}")
                elif arg.startswith("--filesystem="):
                    path = arg.split('=')[1]
                    if path == "home": binds_texts.append("~/")
                    elif path == "xdg-download": binds_texts.append("~/Downloads")
                    elif path == "xdg-pictures": binds_texts.append("~/Pictures")
                    elif path == "xdg-music": binds_texts.append("~/Music")
                    elif path == "xdg-videos": binds_texts.append("~/Videos")
                    elif path == "xdg-documents": binds_texts.append("~/Documents")
                    elif path == "host": binds_texts.append("/")
                    else: binds_texts.append(path)

            self.edit_dbus.setPlainText("\n".join(dbus_texts))
            self.edit_binds.setPlainText("\n".join(binds_texts))
            QMessageBox.information(self, "Loaded", "Profile loaded from your local offline database.")
            return

        # Fetch INI metadata asynchronously using the new Worker
        self.status_label.setText(f"⏳ Fetching INI metadata for {app_id} from Flathub...")
        self.btn_load_profile.setEnabled(False) # Lock the button to prevent spamming
        QApplication.processEvents()

        self.profile_worker = FlatpakProfileWorker(app_id)
        self.profile_worker.finished_success.connect(self.on_profile_fetched)
        self.profile_worker.finished_error.connect(self.on_profile_error)
        self.profile_worker.start()

    def on_profile_fetched(self, app_id, metadata_content):
        self.btn_load_profile.setEnabled(True)
        dbus_texts = []
        binds_texts = []

        # Magic INI parsing
        parser = configparser.ConfigParser()
        parser.optionxform = str # Prevent python from lowercasing DBus paths
        parser.read_string(metadata_content)

        if parser.has_section("Context"):
            shared = parser.get("Context", "shared", fallback="").split(";")
            if "network" in shared: self.chk_network.setChecked(True)
            if "ipc" in shared: self.chk_ipc.setChecked(False)

            sockets = parser.get("Context", "sockets", fallback="").split(";")
            if "wayland" in sockets: self.chk_wayland.setChecked(True)
            if "x11" in sockets or "fallback-x11" in sockets: self.chk_x11.setChecked(True)
            if "pulseaudio" in sockets: self.chk_pulseaudio.setChecked(True)

            devices = parser.get("Context", "devices", fallback="").split(";")
            if "dri" in devices: self.chk_dri.setChecked(True)

            filesystems = parser.get("Context", "filesystems", fallback="").split(";")
            for fs in filesystems:
                if not fs: continue
                path = fs.split(":")[0]
                if path == "home": binds_texts.append("~/")
                elif path == "xdg-download": binds_texts.append("~/Downloads")
                elif path == "xdg-pictures": binds_texts.append("~/Pictures")
                elif path == "xdg-music": binds_texts.append("~/Music")
                elif path == "xdg-videos": binds_texts.append("~/Videos")
                elif path == "xdg-documents": binds_texts.append("~/Documents")
                elif path == "host": binds_texts.append("/")
                else: binds_texts.append(path)

        if parser.has_section("Session Bus Policy"):
            self.chk_dbus_user.setChecked(True)
            for bus_name, policy in parser.items("Session Bus Policy"):
                if policy == "talk": dbus_texts.append(f"--talk={bus_name}")
                elif policy == "own": dbus_texts.append(f"--own={bus_name}")

        self.edit_dbus.setPlainText("\n".join(dbus_texts))
        self.edit_binds.setPlainText("\n".join(binds_texts))

        self.status_label.setText("✅ Profile applied.")
        QMessageBox.information(self, "Success", f"Metadata for {app_id} successfully parsed from Flathub!")

    def on_profile_error(self, error_msg):
        self.btn_load_profile.setEnabled(True)
        self.status_label.setText("❌ Profile fetch failed.")
        QMessageBox.warning(self, "Error", error_msg)

    def on_category_changed(self, index):
        self.package_model.removeRows(0, self.package_model.rowCount())
        category = self.category_list.item(index).text()
        self.tab_permissions.setEnabled(False)

        if "Groups" in category: self.package_model.setHorizontalHeaderLabels(["ID", "Type", "Name", "Installed"])
        elif "Updates" in category: self.package_model.setHorizontalHeaderLabels(["Name", "Installed Version", "Latest Version", "State"])
        elif "Installed" in category: self.package_model.setHorizontalHeaderLabels(["Name", "Version [Mode]", "Location", "State"])
        else: self.package_model.setHorizontalHeaderLabels(["Name", "Version", "Repository", "State"])

        if "Available" in category: self.start_worker(DnfAsyncWorker(task="available"), "Loading available packages...")
        elif "Groups" in category: self.start_worker(DnfAsyncWorker(task="groups"), "Loading Package Groups...")
        elif "Installed" in category: self.start_worker(DnfAsyncWorker(task="installed"), "Scanning installed apps...")
        elif "Updates" in category: self.start_worker(DnfAsyncWorker(task="updates"), "Checking for updates...")
        elif "Queue" in category: self.show_queue()

    def start_worker(self, worker_instance, message):
        if getattr(self, 'worker', None) and self.worker.isRunning(): return
        self.status_label.setText(f"⏳ {message}")
        self.worker = worker_instance
        self.worker.packages_loaded.connect(self.on_packages_batch_loaded)
        if hasattr(self.worker, 'groups_loaded'): self.worker.groups_loaded.connect(self.on_packages_batch_loaded)
        self.worker.finished.connect(lambda: self.status_label.setText("✅ Load complete."))
        self.worker.error.connect(lambda e: self.status_label.setText(f"❌ Error: {e}"))
        self.worker.start()

    def on_packages_batch_loaded(self, batch):
        for pkg in batch:
            self.package_model.appendRow([QStandardItem(str(item)) for item in pkg])

    def on_table_selection(self, selected, deselected):
        indexes = self.package_table.selectionModel().selectedRows()
        if not indexes: return

        real_index = self.proxy_model.mapToSource(indexes[0])
        name = self.package_model.item(real_index.row(), 0).text()
        item_type = self.package_model.item(real_index.row(), 1).text()
        state = self.package_model.item(real_index.row(), 3).text()

        if item_type == "Group":
            self.tab_info.setHtml(f"<h3>Loading contents for {name}...</h3>")
            self.worker_details = DnfAsyncWorker(task="group_details", group_name=name)
            self.worker_details.group_details_loaded.connect(self.on_group_details_loaded)
            self.worker_details.start()
        else:
            self.tab_info.setHtml(f"<h3>{name}</h3><p>Right-click to manage Transaction Queue.</p>")

        if state == "Installed" or "Update Available" in state:
            self.selected_installed_app = name
            self.load_permissions(name)
        else:
            self.selected_installed_app = None
            self.tab_permissions.setEnabled(False)
            self.edit_binds.clear()
            self.edit_masks.clear()

    def on_group_details_loaded(self, packages):
        html = f"<h3>Group Contents</h3><ul>" + "".join([f"<li>{p}</li>" for p in packages]) + "</ul>"
        self.tab_info.setHtml(html)

    def load_permissions(self, app_name):
        self.tab_permissions.setEnabled(True)

        # Reset UI to paranoid default state before loading
        self.chk_network.setChecked(False)
        self.chk_wayland.setChecked(False)
        self.chk_x11.setChecked(False)
        self.chk_pulseaudio.setChecked(False)
        self.chk_dri.setChecked(False)
        self.chk_ipc.setChecked(True)
        self.chk_dbus_user.setChecked(False)
        self.chk_nosandbox.setChecked(False)
        self.edit_dbus.clear()
        self.edit_binds.clear()
        self.edit_masks.clear()

        app_dir = os.path.expanduser(f"~/.var/app/{app_name}")
        metadata_file = os.path.join(app_dir, "metadata")

        if not os.path.exists(metadata_file):
            return

        parser = configparser.ConfigParser()
        parser.optionxform = str # Prevent python from lowercasing DBus paths

        try:
            parser.read(metadata_file)
        except Exception as e:
            QMessageBox.warning(self, "Parse Error", f"Failed to read metadata for {app_name}:\n{e}")
            return

        dbus_texts = []
        binds_texts = []

        if parser.has_section("Context"):
            shared = parser.get("Context", "shared", fallback="").split(";")
            if "network" in shared: self.chk_network.setChecked(True)
            if "ipc" in shared: self.chk_ipc.setChecked(False)

            sockets = parser.get("Context", "sockets", fallback="").split(";")
            if "wayland" in sockets: self.chk_wayland.setChecked(True)
            if "x11" in sockets or "fallback-x11" in sockets: self.chk_x11.setChecked(True)
            if "pulseaudio" in sockets: self.chk_pulseaudio.setChecked(True)

            devices = parser.get("Context", "devices", fallback="").split(";")
            if "dri" in devices: self.chk_dri.setChecked(True)

            filesystems = parser.get("Context", "filesystems", fallback="").split(";")
            for fs in filesystems:
                if fs: binds_texts.append(fs)

        if parser.has_section("Session Bus Policy"):
            self.chk_dbus_user.setChecked(True)
            for bus_name, policy in parser.items("Session Bus Policy"):
                dbus_texts.append(f"--{policy}={bus_name}")

        # Custom section for our Sysext specific features
        if parser.has_section("Sysext"):
            if parser.getboolean("Sysext", "nosandbox", fallback=False):
                self.chk_nosandbox.setChecked(True)

            masks = parser.get("Sysext", "masks", fallback="").split(";")
            self.edit_masks.setPlainText("\n".join([m for m in masks if m]))

        self.edit_dbus.setPlainText("\n".join(dbus_texts))
        self.edit_binds.setPlainText("\n".join(binds_texts))

    def save_permissions(self):
        if not self.selected_installed_app: return

        app_dir = os.path.expanduser(f"~/.var/app/{self.selected_installed_app}")
        os.makedirs(app_dir, exist_ok=True)
        metadata_file = os.path.join(app_dir, "metadata")

        parser = configparser.ConfigParser()
        parser.optionxform = str

        # 1. Build [Context] section
        parser.add_section("Context")

        shared = []
        if self.chk_network.isChecked(): shared.append("network")
        if not self.chk_ipc.isChecked(): shared.append("ipc")
        if shared: parser.set("Context", "shared", ";".join(shared) + ";")

        sockets = []
        if self.chk_wayland.isChecked(): sockets.append("wayland")
        if self.chk_x11.isChecked(): sockets.append("fallback-x11")
        if self.chk_pulseaudio.isChecked(): sockets.append("pulseaudio")
        if sockets: parser.set("Context", "sockets", ";".join(sockets) + ";")

        devices = []
        if self.chk_dri.isChecked(): devices.append("dri")
        if devices: parser.set("Context", "devices", ";".join(devices) + ";")

        binds = [b.strip() for b in self.edit_binds.toPlainText().splitlines() if b.strip()]
        if binds: parser.set("Context", "filesystems", ";".join(binds) + ";")

        # 2. Build [Session Bus Policy] section
        dbus_lines = [d.strip() for d in self.edit_dbus.toPlainText().splitlines() if d.strip()]
        if dbus_lines:
            parser.add_section("Session Bus Policy")
            for line in dbus_lines:
                if line.startswith("--talk="):
                    parser.set("Session Bus Policy", line.split("=")[1], "talk")
                elif line.startswith("--own="):
                    parser.set("Session Bus Policy", line.split("=")[1], "own")

        # 3. Build custom [Sysext] section
        parser.add_section("Sysext")
        if self.chk_nosandbox.isChecked():
            parser.set("Sysext", "nosandbox", "true")

        masks = [m.strip() for m in self.edit_masks.toPlainText().splitlines() if m.strip()]
        if masks:
            parser.set("Sysext", "masks", ";".join(masks) + ";")

        # Write to file
        try:
            with open(metadata_file, "w") as f:
                parser.write(f)
            QMessageBox.information(self, "Saved", f"Metadata saved successfully for {self.selected_installed_app}!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save metadata:\n{e}")

    def show_context_menu(self, position):
        indexes = self.package_table.selectionModel().selectedRows()
        if not indexes: return
        menu = QMenu()

        real_index = self.proxy_model.mapToSource(indexes[0])
        name = self.package_model.item(real_index.row(), 0).text()
        state = self.package_model.item(real_index.row(), 3).text()

        if self.category_list.currentRow() == 4:
            remove_action = QAction("❌ Remove from Queue", self)
            remove_action.triggered.connect(lambda: self.remove_selected_from_queue(indexes))
            menu.addAction(remove_action)
        elif state == "Installed" or "Update Available" in state:
            uninstall_action = QAction("🗑️ Uninstall Application", self)
            uninstall_action.triggered.connect(lambda: self.uninstall_app(name))
            menu.addAction(uninstall_action)
        else:
            add_action = QAction("🛒 Add to Transaction Queue", self)
            add_action.triggered.connect(lambda: self.add_selected_to_queue(indexes))
            menu.addAction(add_action)

        menu.exec(self.package_table.viewport().mapToGlobal(position))

    def uninstall_app(self, app_name):
        msg = f"Are you absolutely sure you want to completely obliterate '{app_name}'?\nThis will delete the application, its settings, and all sandbox data."
        reply = QMessageBox.question(self, 'Confirmation', msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            try:
                # 1. Remove the executable app
                app_path = os.path.expanduser(f"~/.local/bin/{app_name}.app")
                if os.path.exists(app_path): os.remove(app_path)

                # 2. Obliterate sandbox and settings
                var_path = os.path.expanduser(f"~/.var/app/{app_name}")
                if os.path.isdir(var_path): shutil.rmtree(var_path)

                # 3. Delete desktop entry
                desktop_path = os.path.expanduser(f"~/.local/share/applications/{app_name}.desktop")
                if os.path.exists(desktop_path): os.remove(desktop_path)

                # 4. Refresh desktop database
                subprocess.run(["update-desktop-database", os.path.expanduser("~/.local/share/applications")], capture_output=True)

                self.status_label.setText(f"🔥 '{app_name}' has been completely removed.")
                QMessageBox.information(self, "Uninstalled", f"Application '{app_name}' and its environment were successfully deleted.")
                self.on_category_changed(self.category_list.currentRow())
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to uninstall application: {e}")

    def add_selected_to_queue(self, indexes):
        for index in indexes:
            real_index = self.proxy_model.mapToSource(index)
            name = self.package_model.item(real_index.row(), 0).text()
            if name not in self.transaction_queue:
                self.transaction_queue.append(name)
                self.package_model.setItem(real_index.row(), 3, QStandardItem("Queued 🛒"))
        self.update_queue_ui()

    def remove_selected_from_queue(self, indexes):
        names_to_remove = []
        for index in indexes:
            real_index = self.proxy_model.mapToSource(index)
            name = self.package_model.item(real_index.row(), 0).text()
            names_to_remove.append(name)

        for name in names_to_remove:
            if name in self.transaction_queue:
                self.transaction_queue.remove(name)

        self.package_model.removeRows(0, self.package_model.rowCount())
        self.show_queue()
        self.update_queue_ui()

    def update_queue_ui(self):
        count = len(self.transaction_queue)
        self.category_list.item(4).setText(f"🛒 Transaction Queue ({count})")
        self.btn_apply.setEnabled(count > 0)
        self.btn_apply.setText(f"Apply Transaction ({count} items)" if count > 0 else "Apply Transaction")

    def show_queue(self):
        for name in self.transaction_queue:
            self.package_model.appendRow([QStandardItem(name), QStandardItem("pending"), QStandardItem("queue"), QStandardItem("Queued")])

    def apply_transaction(self):
        if not self.transaction_queue: return
        reply = QMessageBox.question(self, 'Confirm', f"Process {len(self.transaction_queue)} packages?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            self.btn_apply.setEnabled(False)
            self.category_list.setEnabled(False)
            self.combo_mode.setEnabled(False)
            self.chk_merge_queue.setEnabled(False)

            selected_mode = "Host-Native" if "Host-Native" in self.combo_mode.currentText() else "Portable"
            merge_queue = self.chk_merge_queue.isChecked()

            self.status_label.setText(f"⚙️ Building in progress ({selected_mode})... Please wait.")
            self.details_tabs.setCurrentIndex(0)
            self.tab_info.setHtml("<h3>Build Log</h3>")

            self.build_worker = BuildAsyncWorker(self.transaction_queue.copy(), selected_mode, merge_queue)
            self.build_worker.log_msg.connect(self.append_build_log)
            self.build_worker.finished_all.connect(self.on_build_finished)
            self.build_worker.error.connect(self.on_build_error)
            self.build_worker.start()

    def append_build_log(self, message):
        self.tab_info.append(message)
        scrollbar = self.tab_info.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def on_build_finished(self):
        self.status_label.setText("✅ Transaction complete.")
        QMessageBox.information(self, "Success", "All queued applications have been processed successfully!")
        self.transaction_queue.clear()
        self.update_queue_ui()
        self.category_list.setEnabled(True)
        self.combo_mode.setEnabled(True)
        self.chk_merge_queue.setEnabled(True)
        self.on_category_changed(self.category_list.currentRow())

    def on_build_error(self, error_message):
        self.status_label.setText("❌ Build Error")
        QMessageBox.critical(self, "Build Error", error_message)
        self.category_list.setEnabled(True)
        self.combo_mode.setEnabled(True)
        self.chk_merge_queue.setEnabled(True)
        self.update_queue_ui()

    def closeEvent(self, event):
        if getattr(self, 'init_worker', None) and self.init_worker.isRunning(): self.init_worker.terminate()
        if getattr(self, 'worker', None) and self.worker.isRunning(): self.worker.terminate()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SysextAdvancedGUI()
    window.show()
    sys.exit(app.exec())
