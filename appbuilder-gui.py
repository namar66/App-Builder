#!/usr/bin/env python3

# Modern Host-Only App Manager (Host GUI)
# Runs on the host system but executes DNF and Builder inside a toolbox.

import sys
import os
import subprocess
import logging
import re
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QSplitter, QListWidget, QTableView,
                             QLineEdit, QTabWidget, QTextEdit, QLabel, QPushButton,
                             QHeaderView, QMenu, QMessageBox, QAbstractItemView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QAction

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# Configuration: Define which toolbox handles the building
TOOLBOX_NAME = "devel"

# ==========================================
# ASYNC BUILDER THREAD
# ==========================================
class AppBuildWorker(QThread):
    output_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, app_name, packages):
        super().__init__()
        self.app_name = app_name
        self.packages = packages

    def run(self):
        self.output_signal.emit(f"Delegating build for {self.app_name} to toolbox '{TOOLBOX_NAME}'...\n")

        # We must provide the absolute path to the builder script so toolbox can find it.
        builder_script = os.path.abspath("./appimage-builder.py")
        if not os.path.exists(builder_script):
            self.finished_signal.emit(False, f"Builder script not found at {builder_script}")
            return

        # Execute inside toolbox
        cmd = ["toolbox", "run", "-c", TOOLBOX_NAME, builder_script, self.app_name] + self.packages

        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                self.output_signal.emit(line.strip())
            process.wait()

            if process.returncode != 0:
                self.finished_signal.emit(False, f"Build failed with exit code {process.returncode}")
                return

            self.finished_signal.emit(True, f"Successfully built and installed {self.app_name}!\nCheck your application menu.")

        except Exception as e:
            self.finished_signal.emit(False, f"Critical error during build delegation: {e}")


# ==========================================
# ASYNC DNF WORKER THREAD
# ==========================================
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
        logging.info(f"[WORKER] Fetching DNF groups from toolbox {TOOLBOX_NAME}...")
        cmd = ["toolbox", "run", "-c", TOOLBOX_NAME, "dnf", "group", "list", "--hidden"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                self.error.emit(f"DNF Group List Error: {res.stderr.strip()}")
                return

            groups = []
            for line in res.stdout.splitlines():
                clean_line = line.strip()
                if not clean_line or clean_line.startswith((
                    "ID", "Available", "Installed", "Hidden", "Environment",
                    "Last metadata", "Aktualizace", "Repozitáře"
                )):
                    continue

                parts = re.split(r'\s{2,}', clean_line)
                if len(parts) >= 3:
                    groups.append([parts[0], "Group", parts[1], parts[2]])
                elif len(parts) == 2:
                    groups.append([parts[0], "Group", parts[1], "Unknown"])

            self.groups_loaded.emit(groups)
        except Exception as e:
            self.error.emit(f"Failed to load groups: {e}")

    def load_group_details(self):
        logging.info(f"[WORKER] Fetching details for group: {self.group_name}")
        host_installed = self.get_host_installed_packages()
        cmd = ["toolbox", "run", "-c", TOOLBOX_NAME, "env", "LANG=C", "dnf", "group", "info", "--quiet", self.group_name]

        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            packages = []
            parsing_target = False

            for line in res.stdout.splitlines():
                if not line.strip(): continue
                if ":" in line:
                    left_part, right_part = line.split(":", 1)
                    left_clean = left_part.strip()
                    right_clean = right_part.strip()

                    if left_clean in ["Mandatory packages", "Default packages"]:
                        parsing_target = True
                        if right_clean and right_clean not in host_installed:
                            packages.append(right_clean)
                    elif left_clean == "":
                        if parsing_target and right_clean and right_clean not in host_installed:
                            packages.append(right_clean)
                    else:
                        parsing_target = False

            self.group_details_loaded.emit(packages)
        except Exception as e:
            self.error.emit(f"Failed to load group details: {e}")

    def get_host_installed_packages(self) -> set:
        """Fetch RPMs natively from the host since the GUI is running on the host."""
        try:
            cmd = ["rpm", "-qa", "--queryformat", "%{NAME}\n"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return set(res.stdout.splitlines())
        except Exception as e:
            logging.warning(f"Failed to fetch host packages: {e}")
            return set()

    def load_available_packages(self):
        logging.info("[WORKER] Loading installed packages from host...")
        host_installed = self.get_host_installed_packages()

        cmd = ["toolbox", "run", "-c", TOOLBOX_NAME, "dnf", "repoquery", "--quiet", "--queryformat", "%{name}|%{version}-%{release}|%{repoid}\n"]
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            batch = []

            for line in process.stdout:
                clean_line = line.strip()
                if not clean_line or "Last metadata" in clean_line: continue

                parts = clean_line.split("|")
                if len(parts) == 3:
                    name, version, repo = parts
                    if name not in host_installed:
                        batch.append([name, version, repo, "Available"])

                if len(batch) >= 500:
                    self.packages_loaded.emit(batch)
                    batch = []

            if batch:
                self.packages_loaded.emit(batch)
            process.wait()

            if process.returncode != 0:
                self.error.emit(f"DNF failed with code {process.returncode}.")

        except Exception as e:
            self.error.emit(f"Error reading from DNF: {e}")

    def run(self):
        if self.task == "available":
            self.load_available_packages()
        elif self.task == "groups":
            self.load_groups()
        elif self.task == "group_details":
            self.load_group_details()
        self.finished.emit()


# ==========================================
# MAIN GUI CLASS
# ==========================================
class AppManagerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Host-Only App Manager (Toolbox Backend: {TOOLBOX_NAME})")
        self.resize(1100, 750)

        self.transaction_queue = []
        self.worker = None
        self.build_worker = None

        self.setup_ui()

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
        self.category_list.addItems([
            "📦 Available (DNF)",
            "📁 Package Groups",
            "🛒 Transaction Queue (0)"
        ])
        self.category_list.currentRowChanged.connect(self.on_category_changed)

        left_layout.addWidget(QLabel("<b>Categories</b>"))
        left_layout.addWidget(self.category_list)

        self.btn_apply = QPushButton("Queue is empty (Right-click apps to add)")
        self.btn_apply.setEnabled(False)
        self.btn_apply.setStyleSheet("background-color: #7f8c8d; color: #ecf0f1; font-weight: bold; padding: 10px;")
        self.btn_apply.clicked.connect(self.apply_transaction)
        left_layout.addWidget(self.btn_apply)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        top_right_panel = QWidget()
        top_right_layout = QVBoxLayout(top_right_panel)
        top_right_layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Select a category to begin.")
        self.status_label.setStyleSheet("color: gray; font-style: italic;")
        top_right_layout.addWidget(self.status_label)

        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Live Search...")
        top_right_layout.addWidget(self.search_bar)

        self.package_table = QTableView()
        self.package_model = QStandardItemModel(0, 4)
        self.package_model.setHorizontalHeaderLabels(["Name", "Version", "Repository", "State"])

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

        top_right_layout.addWidget(self.package_table)

        self.details_tabs = QTabWidget()
        self.tab_info = QTextEdit()
        self.tab_info.setReadOnly(True)
        self.details_tabs.addTab(self.tab_info, "Terminal / Information")

        right_splitter.addWidget(top_right_panel)
        right_splitter.addWidget(self.details_tabs)
        right_splitter.setSizes([500, 200])

        main_splitter.addWidget(left_panel)
        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([250, 850])
        self.package_table.selectionModel().selectionChanged.connect(self.on_table_selection)

    def on_category_changed(self, index):
        self.package_model.removeRows(0, self.package_model.rowCount())
        category = self.category_list.item(index).text()

        if "Groups" in category:
            self.package_model.setHorizontalHeaderLabels(["ID", "Type", "Name", "Installed"])
            self.start_loading_groups()
        elif "Queue" in category:
            self.package_model.setHorizontalHeaderLabels(["Name", "Version", "Repository", "State"])
            self.show_queue()
        else:
            self.package_model.setHorizontalHeaderLabels(["Name", "Version", "Repository", "State"])
            self.start_loading_available()

    def start_loading_available(self):
        if getattr(self, 'worker', None) and self.worker.isRunning(): return
        self.status_label.setText("⏳ Loading available packages via Toolbox...")
        self.worker = DnfAsyncWorker(task="available")
        self.worker.packages_loaded.connect(self.on_packages_batch_loaded)
        self.worker.finished.connect(lambda: self.status_label.setText(f"✅ Loaded {self.package_model.rowCount()} packages."))
        self.worker.error.connect(lambda e: self.status_label.setText(f"❌ Error: {e}"))
        self.worker.start()

    def on_packages_batch_loaded(self, batch):
        for pkg in batch:
            row = [QStandardItem(str(item)) for item in pkg]
            self.package_model.appendRow(row)

    def start_loading_groups(self):
        if getattr(self, 'worker', None) and self.worker.isRunning(): return
        self.status_label.setText("⏳ Loading Package Groups via Toolbox...")
        self.worker = DnfAsyncWorker(task="groups")
        self.worker.groups_loaded.connect(self.on_packages_batch_loaded)
        self.worker.finished.connect(lambda: self.status_label.setText(f"✅ Loaded {self.package_model.rowCount()} groups."))
        self.worker.error.connect(lambda e: self.status_label.setText(f"❌ Error: {e}"))
        self.worker.start()

    def on_table_selection(self, selected, deselected):
        indexes = self.package_table.selectionModel().selectedRows()
        if not indexes: return

        real_index = self.proxy_model.mapToSource(indexes[0])
        name = self.package_model.item(real_index.row(), 0).text()

        if self.package_model.columnCount() > 1 and self.package_model.item(real_index.row(), 1):
            item_type = self.package_model.item(real_index.row(), 1).text()
            if item_type == "Group":
                self.tab_info.setHtml(f"<h3>Loading contents for {name}...</h3>")
                self.worker_details = DnfAsyncWorker(task="group_details", group_name=name)
                self.worker_details.group_details_loaded.connect(self.on_group_details_loaded)
                self.worker_details.start()
                return

        self.tab_info.setHtml(f"<h3>{name}</h3><p>Right-click to add this package to your build queue.</p>")

    def on_group_details_loaded(self, packages):
        html = f"<h3>Group Contents ({len(packages)} packages)</h3><ul>"
        for p in packages: html += f"<li>{p}</li>"
        html += "</ul><p><i><b>Right-click the group in the table</b> to add all these packages!</i></p>"
        self.tab_info.setHtml(html)
        self.current_group_packages = packages

    def show_context_menu(self, position):
        indexes = self.package_table.selectionModel().selectedRows()
        if not indexes: return

        menu = QMenu()
        add_action = QAction("🛒 Add to Build Queue", self)
        add_action.triggered.connect(lambda: self.add_selected_to_queue(indexes))
        menu.addAction(add_action)
        menu.exec(self.package_table.viewport().mapToGlobal(position))

    def add_selected_to_queue(self, indexes):
        for index in indexes:
            real_index = self.proxy_model.mapToSource(index)
            name = self.package_model.item(real_index.row(), 0).text()
            item_type = self.package_model.item(real_index.row(), 1).text() if self.package_model.item(real_index.row(), 1) else ""

            if item_type == "Group":
                for pkg in getattr(self, 'current_group_packages', []):
                    if pkg not in self.transaction_queue:
                        self.transaction_queue.append(pkg)
            else:
                if name not in self.transaction_queue:
                    self.transaction_queue.append(name)
                    self.package_model.setItem(real_index.row(), 3, QStandardItem("Queued 🛒"))

        self.update_queue_ui()

    def update_queue_ui(self):
        count = len(self.transaction_queue)
        self.category_list.item(2).setText(f"🛒 Transaction Queue ({count})")

        if count > 0:
            self.btn_apply.setEnabled(True)
            self.btn_apply.setText(f"Build App ({count} packages)")
        else:
            self.btn_apply.setEnabled(False)
            self.btn_apply.setText("Queue is empty (Right-click apps to add)")
            # Optional: Make it look disabled/grayed out
            self.btn_apply.setStyleSheet("background-color: #7f8c8d; color: #bdc3c7; font-weight: bold; padding: 10px;")

    def show_queue(self):
        self.status_label.setText("Review your pending application build.")
        for name in self.transaction_queue:
            row = [
                QStandardItem(name),
                QStandardItem("pending"),
                QStandardItem("queue"),
                QStandardItem("To be built")
            ]
            self.package_model.appendRow(row)

    def apply_transaction(self):
        if not self.transaction_queue: return

        primary_app = self.transaction_queue[0]

        reply = QMessageBox.question(
            self, 'Confirm Build',
            f"Do you want to build '{primary_app}.app' using toolbox '{TOOLBOX_NAME}'?\n\nPackages: " + ", ".join(self.transaction_queue),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.btn_apply.setEnabled(False)
            self.tab_info.clear()
            self.status_label.setText(f"Building {primary_app} inside toolbox...")

            self.build_worker = AppBuildWorker(primary_app, self.transaction_queue)
            self.build_worker.output_signal.connect(self.append_terminal_output)
            self.build_worker.finished_signal.connect(self.on_build_finished)
            self.build_worker.start()

    def append_terminal_output(self, text):
        self.tab_info.append(text)
        scrollbar = self.tab_info.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def on_build_finished(self, success, message):
        self.btn_apply.setEnabled(True)
        self.status_label.setText("Build finished.")

        if success:
            QMessageBox.information(self, "Success", message)
            self.transaction_queue.clear()
            self.update_queue_ui()
            self.on_category_changed(self.category_list.currentRow())
        else:
            QMessageBox.critical(self, "Error", message)

    def closeEvent(self, event):
        try:
            for worker in [self.worker, self.build_worker]:
                if worker and worker.isRunning():
                    worker.terminate()
                    worker.wait(1000)
        except Exception:
            pass
        finally:
            event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AppManagerGUI()
    window.show()
    sys.exit(app.exec())
