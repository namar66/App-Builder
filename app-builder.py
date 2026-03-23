#!/usr/bin/env python3

# Modern Host-Only App Builder
# Generates a standalone executable, extracts native icons, and creates menu entries.
# Requires: gcc, mkfs.erofs, rpm, cpio, dnf

import os
import sys
import subprocess
import shutil
import logging
import re
import struct

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def run_cmd(cmd, cwd=None):
    try:
        return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, errors="replace")
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {' '.join(cmd)}\nError: {e.stderr}")
        sys.exit(1)

def calculate_host_dependencies(packages):
    if not packages:
        return []
    logging.info("Calculating missing dependencies against host OS...")
    # Since builder runs in toolbox, we use flatpak-spawn to ask the host
    cmd = ["flatpak-spawn", "--host", "rpm-ostree", "install", "--dry-run"] + packages
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        added_pkgs = []
        parsing_added = False
        nevra_re = re.compile(r'^(.+?)-(([0-9]+:)?([^-]+)-([^-]+))\.(x86_64|noarch|i686)$')

        for line in res.stdout.splitlines():
            if any(line.startswith(s) for s in ["Installing ", "Added:", "Upgrading ", "Upgraded:"]):
                parsing_added = True
                continue
            elif any(line.startswith(s) for s in ["Removing ", "Removed:"]):
                parsing_added = False
                continue

            if parsing_added and line.startswith(" "):
                raw_pkg = line.strip().split()[0]
                match = nevra_re.match(raw_pkg)
                if match:
                    name_pkg, arch = match.group(1), match.group(6)
                    formatted = f"{name_pkg}.{arch}"
                    if formatted not in added_pkgs:
                        added_pkgs.append(formatted)
        return added_pkgs
    except:
        return packages

def generate_c_wrapper(source_path, entrypoint_suffix):
    c_code = f"""
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <stdint.h>
#include <string.h>

// Footer structure at the end of the binary.
typedef struct __attribute__((packed)) {{
    uint64_t erofs_offset;
    uint32_t magic; // 0x41505049 (APPI)
}} app_footer_t;

int main(int argc, char *argv[]) {{
    char self_path[4096];
    ssize_t len = readlink("/proc/self/exe", self_path, sizeof(self_path) - 1);
    if (len == -1) return 1;
    self_path[len] = '\\0';

    int fd_self = open(self_path, O_RDONLY);
    if (fd_self < 0) return 1;

    struct stat st;
    if (fstat(fd_self, &st) != 0) return 1;

    app_footer_t footer;
    if (lseek(fd_self, st.st_size - sizeof(app_footer_t), SEEK_SET) == -1) return 1;
    if (read(fd_self, &footer, sizeof(app_footer_t)) != sizeof(app_footer_t)) return 1;

    if (footer.magic != 0x41505049) {{
        fprintf(stderr, "Error: Magic number mismatch.\\n");
        return 1;
    }}
    close(fd_self);

    char mount_point[] = "/tmp/appmnt.XXXXXX";
    if (mkdtemp(mount_point) == NULL) return 1;

    char offset_str[64];
    snprintf(offset_str, sizeof(offset_str), "--offset=%llu", (unsigned long long)footer.erofs_offset);

    pid_t fuse_pid = fork();
    if (fuse_pid == 0) {{
        int dev_null = open("/dev/null", O_WRONLY);
        if (dev_null != -1) {{
            dup2(dev_null, STDOUT_FILENO);
            dup2(dev_null, STDERR_FILENO);
            close(dev_null);
        }}
        char *fuse_args[] = {{"erofsfuse", offset_str, self_path, mount_point, NULL}};
        execvp(fuse_args[0], fuse_args);
        exit(1);
    }}

    usleep(250000);

    pid_t app_pid = fork();
    if (app_pid == 0) {{
        char path_env[2048], ld_env[2048], xdg_env[2048], entrypoint_path[2048];
        snprintf(path_env, sizeof(path_env), "%s/usr/bin:/usr/bin:/bin", mount_point);
        snprintf(ld_env, sizeof(ld_env), "%s/usr/lib64:%s/usr/lib:/usr/lib64:/usr/lib", mount_point, mount_point);
        snprintf(xdg_env, sizeof(xdg_env), "%s/usr/share:/usr/share", mount_point);
        snprintf(entrypoint_path, sizeof(entrypoint_path), "%s%s", mount_point, "{entrypoint_suffix}");

        char *home = getenv("HOME");
        if (!home) home = "/";

        char *bwrap_args[128] = {{
            "bwrap",
            "--ro-bind", "/", "/",
            "--dev-bind", "/dev", "/dev",
            "--proc", "/proc",
            "--ro-bind", "/sys", "/sys",
            "--bind", "/tmp", "/tmp",
            "--bind", "/run", "/run",
            "--bind", home, home,
            "--setenv", "PATH", path_env,
            "--setenv", "LD_LIBRARY_PATH", ld_env,
            "--setenv", "XDG_DATA_DIRS", xdg_env,
            entrypoint_path,
            NULL
        }};

        int arg_idx = 0;
        while (bwrap_args[arg_idx] != NULL) arg_idx++;
        for (int i = 1; i < argc && arg_idx < 127; i++) bwrap_args[arg_idx++] = argv[i];
        bwrap_args[arg_idx] = NULL;

        execvp(bwrap_args[0], bwrap_args);
        exit(1);
    }}

    int status;
    waitpid(app_pid, &status, 0);

    pid_t umount_pid = fork();
    if (umount_pid == 0) {{
        int dev_null = open("/dev/null", O_WRONLY);
        if (dev_null != -1) {{
            dup2(dev_null, STDOUT_FILENO);
            dup2(dev_null, STDERR_FILENO);
            close(dev_null);
        }}
        char *umount_args[] = {{"fusermount3", "-q", "-u", mount_point, NULL}};
        execvp(umount_args[0], umount_args);
        exit(1);
    }}

    waitpid(umount_pid, NULL, 0);
    rmdir(mount_point);

    return WEXITSTATUS(status);
}}
"""
    with open(source_path, "w") as f:
        f.write(c_code)

def stitch_app(output_app_path, runtime_path, erofs_path):
    with open(output_app_path, "wb") as f_out:
        with open(runtime_path, "rb") as f_runtime:
            f_out.write(f_runtime.read())
        erofs_offset = f_out.tell()
        with open(erofs_path, "rb") as f_erofs:
            f_out.write(f_erofs.read())
        footer = struct.pack("<QI", erofs_offset, 0x41505049)
        f_out.write(footer)
    os.chmod(output_app_path, 0o755)

def integrate_into_system(app_name, final_app_path, staging_root):
    """
    Extracts native .desktop files and icons from the staging root
    and installs them into the user's home directory.
    """
    logging.info("Extracting native desktop integrations...")
    home = os.path.expanduser("~")
    bin_dir = os.path.join(home, ".local", "bin")
    app_target_dir = os.path.join(home, ".local", "share", "applications")
    icon_target_dir = os.path.join(home, ".local", "share", "icons", "hicolor", "scalable", "apps")

    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(app_target_dir, exist_ok=True)
    os.makedirs(icon_target_dir, exist_ok=True)

    target_bin = os.path.join(bin_dir, f"{app_name}.app")
    shutil.move(final_app_path, target_bin)

    # 1. Hunt for the original .desktop file
    desktop_src = None
    apps_dir = os.path.join(staging_root, "usr", "share", "applications")
    icon_name = app_name

    if os.path.exists(apps_dir):
        for f in os.listdir(apps_dir):
            if f.endswith(".desktop"):
                desktop_src = os.path.join(apps_dir, f)
                # If multiple exist, try to match the exact app name
                if app_name in f:
                    break

    if desktop_src:
        with open(desktop_src, "r") as f:
            lines = f.readlines()

        target_desktop = os.path.join(app_target_dir, f"{app_name}.desktop")
        with open(target_desktop, "w") as f:
            for line in lines:
                if line.startswith("Exec="):
                    f.write(f"Exec={target_bin} %U\n")
                elif line.startswith("TryExec="):
                    continue
                else:
                    if line.startswith("Icon="):
                        icon_name = line.split("=")[1].strip()
                    f.write(line)
    else:
        # Fallback if no desktop file was provided by the RPM
        logging.warning("No native .desktop file found. Creating a generic one.")
        with open(os.path.join(app_target_dir, f"{app_name}.desktop"), "w") as f:
            f.write(f"[Desktop Entry]\nName={app_name.capitalize()}\nExec={target_bin} %U\nIcon={icon_name}\nType=Application\n")

    # 2. Hunt for the icons
    extracted_icons_dir = os.path.join(staging_root, "usr", "share", "icons")
    if os.path.exists(extracted_icons_dir):
        for root, dirs, files in os.walk(extracted_icons_dir):
            for file in files:
                if file.startswith(icon_name) and file.endswith((".png", ".svg")):
                    shutil.copy(os.path.join(root, file), os.path.join(icon_target_dir, file))

    # Optional: Update desktop database if the tool is available in toolbox
    try:
        subprocess.run(["update-desktop-database", app_target_dir], capture_output=True)
    except FileNotFoundError:
        pass

    logging.info(f"Successfully integrated {app_name}. It is now available in your KDE menu.")

def main():
    if len(sys.argv) < 3:
        print("Usage: build-app.py <App-Name> <Package1> [Package2 ...]")
        sys.exit(1)

    name = sys.argv[1]
    requested_packages = sys.argv[2:]

    output_dir = os.path.abspath("./")
    build_dir = f"/var/tmp/app-build-{name}"
    staging_root = os.path.join(build_dir, "root")

    try:
        if os.path.exists(build_dir): shutil.rmtree(build_dir)
        os.makedirs(staging_root, exist_ok=True)

        local_rpms = [p for p in requested_packages if p.endswith(".rpm") and os.path.isfile(p)]
        repo_packages = [p for p in requested_packages if p not in local_rpms]
        missing_deps = calculate_host_dependencies(repo_packages + local_rpms)

        dnf_dir = os.path.join(build_dir, "dnf-downloads")
        os.makedirs(dnf_dir, exist_ok=True)

        if missing_deps:
            logging.info("Downloading missing dependencies...")
            run_cmd(["dnf", "--refresh", "download", "-y", f"--destdir={dnf_dir}"] + missing_deps)

        rpms_to_extract = local_rpms + [os.path.join(dnf_dir, f) for f in os.listdir(dnf_dir) if f.endswith(".rpm")]

        logging.info("Extracting RPMs...")
        for rpm in rpms_to_extract:
            ps = subprocess.Popen(["rpm2cpio", rpm], stdout=subprocess.PIPE)
            subprocess.run(["cpio", "-idmv"], stdin=ps.stdout, cwd=staging_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ps.wait()

        entrypoint_suffix = "/usr/bin/bash"
        bin_dir = os.path.join(staging_root, "usr", "bin")
        if os.path.exists(bin_dir):
            bins = [f for f in os.listdir(bin_dir) if os.path.isfile(os.path.join(bin_dir, f))]
            if bins:
                entrypoint_suffix = f"/usr/bin/{name}" if name in bins else f"/usr/bin/{bins[0]}"

        erofs_payload = os.path.join(build_dir, "payload.erofs")
        selinux_contexts = "/run/host/etc/selinux/targeted/contexts/files/file_contexts"
        cmd_erofs = ["mkfs.erofs", "-x1", "--all-root", "-U", "clear", "-T", "0"]
        if os.path.exists(selinux_contexts): cmd_erofs.append(f"--file-contexts={selinux_contexts}")
        cmd_erofs.extend([erofs_payload, staging_root])

        logging.info("Building EROFS filesystem...")
        run_cmd(cmd_erofs)

        c_source_path = os.path.join(build_dir, "wrapper.c")
        runtime_bin_path = os.path.join(build_dir, "runtime")
        generate_c_wrapper(c_source_path, entrypoint_suffix)
        run_cmd(["gcc", "-O2", c_source_path, "-o", runtime_bin_path])

        final_app_path = os.path.join(output_dir, f"{name}.app")
        stitch_app(final_app_path, runtime_bin_path, erofs_payload)

        # New Step: Integrate .desktop and icons BEFORE cleaning up staging_root
        integrate_into_system(name, final_app_path, staging_root)

    finally:
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)
            logging.info("Cleaned up build directory.")

if __name__ == "__main__":
    main()
