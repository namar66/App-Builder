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

def fix_absolute_symlinks(staging_root):
    logging.info("Fixing absolute symlinks for isolated execution...")
    for root, dirs, files in os.walk(staging_root):
        for name in files + dirs:
            path = os.path.join(root, name)
            if os.path.islink(path):
                target = os.readlink(path)
                if target.startswith('/'):
                    virtual_path = "/" + os.path.relpath(path, staging_root)
                    virtual_dir = os.path.dirname(virtual_path)
                    rel_target = os.path.relpath(target, virtual_dir)
                    os.unlink(path)
                    os.symlink(rel_target, path)

def detect_entrypoint(staging_root, app_name):
    desktop_exec = None
    desktop_files = []
    
    for root, dirs, files in os.walk(staging_root):
        for f in files:
            if f.endswith(".desktop"):
                desktop_files.append(os.path.join(root, f))
                
    desktop_files.sort(key=lambda x: 0 if app_name.lower() in os.path.basename(x).lower() else 1)

    for df_path in desktop_files:
        basename = os.path.basename(df_path).lower()
        if app_name.lower() in basename or len(desktop_files) == 1:
            with open(df_path, 'r', encoding='utf-8', errors='ignore') as df:
                for line in df:
                    if line.startswith("Exec="):
                        cmd = line.strip().split("=", 1)[1]
                        parts = cmd.split()
                        for part in parts:
                            if "=" not in part and part.lower() != "env":
                                desktop_exec = os.path.basename(part)
                                break
                        if not desktop_exec and len(parts) > 0:
                            desktop_exec = os.path.basename(parts[0])
                        break
        if desktop_exec:
            break
            
    targets = []
    if desktop_exec:
        targets.append(desktop_exec)
    targets.append(app_name)

    search_dirs = ["usr/bin", "opt", "usr/libexec", "usr/sbin"]
    for target in targets:
        for sdir in search_dirs:
            full_sdir = os.path.join(staging_root, sdir)
            if os.path.exists(full_sdir):
                for root, dirs, files in os.walk(full_sdir):
                    if target in files:
                        rel_path = os.path.relpath(os.path.join(root, target), staging_root)
                        logging.info(f"Smart-detected entrypoint: /{rel_path}")
                        return "/" + rel_path
                        
    logging.warning("Could not auto-detect entrypoint. Falling back to bash.")
    return "/usr/bin/bash"

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
#include <dirent.h>

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
        char opt_bind_src[2048], libgl_env[2048];
        
        snprintf(path_env, sizeof(path_env), "/app/usr/bin:/usr/bin:/bin");
        snprintf(ld_env, sizeof(ld_env), "/app/usr/lib64:/app/usr/lib:/usr/lib64:/usr/lib");
        snprintf(xdg_env, sizeof(xdg_env), "/app/usr/share:/usr/share");
        snprintf(entrypoint_path, sizeof(entrypoint_path), "/app%s", "{entrypoint_suffix}");
        snprintf(opt_bind_src, sizeof(opt_bind_src), "%s/opt", mount_point);
        snprintf(libgl_env, sizeof(libgl_env), "/app/usr/lib64/dri:/app/usr/lib/dri:/usr/lib64/dri:/usr/lib/dri");

        char *home = getenv("HOME");
        if (!home) home = "/";

        char *bwrap_args[8192];
        int arg_idx = 0;
        
        bwrap_args[arg_idx++] = "bwrap";
        
        bwrap_args[arg_idx++] = "--ro-bind"; bwrap_args[arg_idx++] = "/usr"; bwrap_args[arg_idx++] = "/usr";
        bwrap_args[arg_idx++] = "--ro-bind"; bwrap_args[arg_idx++] = "/etc"; bwrap_args[arg_idx++] = "/etc";
        
        // --- THE SSL ILLUSION FOR UBUNTU BINARIES (STEAM) ---
        bwrap_args[arg_idx++] = "--tmpfs"; bwrap_args[arg_idx++] = "/etc/ssl";
        bwrap_args[arg_idx++] = "--dir"; bwrap_args[arg_idx++] = "/etc/ssl/certs";
        bwrap_args[arg_idx++] = "--symlink"; bwrap_args[arg_idx++] = "/etc/pki/tls/certs/ca-bundle.crt"; bwrap_args[arg_idx++] = "/etc/ssl/certs/ca-certificates.crt";
        bwrap_args[arg_idx++] = "--symlink"; bwrap_args[arg_idx++] = "/etc/pki/tls/certs/ca-bundle.crt"; bwrap_args[arg_idx++] = "/etc/ssl/certs/ca-bundle.crt";

        bwrap_args[arg_idx++] = "--dev-bind"; bwrap_args[arg_idx++] = "/dev"; bwrap_args[arg_idx++] = "/dev";
        bwrap_args[arg_idx++] = "--proc"; bwrap_args[arg_idx++] = "/proc";
        bwrap_args[arg_idx++] = "--ro-bind"; bwrap_args[arg_idx++] = "/sys"; bwrap_args[arg_idx++] = "/sys";
        bwrap_args[arg_idx++] = "--bind"; bwrap_args[arg_idx++] = "/tmp"; bwrap_args[arg_idx++] = "/tmp";
        bwrap_args[arg_idx++] = "--bind"; bwrap_args[arg_idx++] = "/run"; bwrap_args[arg_idx++] = "/run";
        bwrap_args[arg_idx++] = "--bind"; bwrap_args[arg_idx++] = "/var"; bwrap_args[arg_idx++] = "/var";
        bwrap_args[arg_idx++] = "--bind"; bwrap_args[arg_idx++] = home; bwrap_args[arg_idx++] = home;
        
        bwrap_args[arg_idx++] = "--bind-try"; bwrap_args[arg_idx++] = "/mnt"; bwrap_args[arg_idx++] = "/mnt";
        bwrap_args[arg_idx++] = "--bind-try"; bwrap_args[arg_idx++] = "/media"; bwrap_args[arg_idx++] = "/media";

        bwrap_args[arg_idx++] = "--symlink"; bwrap_args[arg_idx++] = "usr/bin"; bwrap_args[arg_idx++] = "/bin";
        bwrap_args[arg_idx++] = "--symlink"; bwrap_args[arg_idx++] = "usr/sbin"; bwrap_args[arg_idx++] = "/sbin";
        bwrap_args[arg_idx++] = "--symlink"; bwrap_args[arg_idx++] = "usr/lib64"; bwrap_args[arg_idx++] = "/lib64";
        
        bwrap_args[arg_idx++] = "--dir"; bwrap_args[arg_idx++] = "/lib";
        DIR *dir = opendir("/usr/lib");
        if (dir) {{
            struct dirent *entry;
            while ((entry = readdir(dir)) != NULL) {{
                if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) continue;
                if (strcmp(entry->d_name, "ld-linux.so.2") == 0) continue;
                
                char *target = malloc(4096);
                snprintf(target, 4096, "usr/lib/%s", entry->d_name);
                char *linkpath = malloc(4096);
                snprintf(linkpath, 4096, "/lib/%s", entry->d_name);
                
                bwrap_args[arg_idx++] = "--symlink";
                bwrap_args[arg_idx++] = target;
                bwrap_args[arg_idx++] = linkpath;
            }}
            closedir(dir);
        }}
        
        bwrap_args[arg_idx++] = "--symlink";
        bwrap_args[arg_idx++] = "/app/usr/lib/ld-linux.so.2";
        bwrap_args[arg_idx++] = "/lib/ld-linux.so.2";

        bwrap_args[arg_idx++] = "--ro-bind";
        bwrap_args[arg_idx++] = mount_point;
        bwrap_args[arg_idx++] = "/app";
        
        bwrap_args[arg_idx++] = "--dir"; bwrap_args[arg_idx++] = "/opt";
        bwrap_args[arg_idx++] = "--ro-bind-try";
        bwrap_args[arg_idx++] = opt_bind_src;
        bwrap_args[arg_idx++] = "/opt";
        
        bwrap_args[arg_idx++] = "--setenv"; bwrap_args[arg_idx++] = "PATH"; bwrap_args[arg_idx++] = path_env;
        bwrap_args[arg_idx++] = "--setenv"; bwrap_args[arg_idx++] = "LD_LIBRARY_PATH"; bwrap_args[arg_idx++] = ld_env;
        bwrap_args[arg_idx++] = "--setenv"; bwrap_args[arg_idx++] = "XDG_DATA_DIRS"; bwrap_args[arg_idx++] = xdg_env;
        bwrap_args[arg_idx++] = "--setenv"; bwrap_args[arg_idx++] = "LIBGL_DRIVERS_PATH"; bwrap_args[arg_idx++] = libgl_env;
        
        bwrap_args[arg_idx++] = "--setenv"; bwrap_args[arg_idx++] = "SSL_CERT_DIR"; bwrap_args[arg_idx++] = "/etc/pki/tls/certs";
        bwrap_args[arg_idx++] = "--setenv"; bwrap_args[arg_idx++] = "SSL_CERT_FILE"; bwrap_args[arg_idx++] = "/etc/pki/tls/certs/ca-bundle.crt";
        bwrap_args[arg_idx++] = "--setenv"; bwrap_args[arg_idx++] = "CURL_CA_BUNDLE"; bwrap_args[arg_idx++] = "/etc/pki/tls/certs/ca-bundle.crt";
        
        bwrap_args[arg_idx++] = entrypoint_path;
        
        // --- SMART SANDBOX FIX FOR STEAM/CHROMIUM ---
        // Prevent steamwebhelper (CEF) from trying to construct a nested sandbox
        if (strstr(entrypoint_path, "steam") != NULL) {{
            bwrap_args[arg_idx++] = "-no-cef-sandbox";
        }}
        
        for (int i = 1; i < argc && arg_idx < 8190; i++) bwrap_args[arg_idx++] = argv[i];
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
    
    desktop_src = None
    icon_name = app_name
    
    search_desktop_dirs = [
        os.path.join(staging_root, "usr", "share", "applications"),
        os.path.join(staging_root, "opt")
    ]
    
    for sdir in search_desktop_dirs:
        if os.path.exists(sdir):
            for root, dirs, files in os.walk(sdir):
                for f in files:
                    if f.endswith(".desktop"):
                        desktop_src = os.path.join(root, f)
                        if app_name in f:
                            break
                if desktop_src: break
        if desktop_src: break

    if desktop_src:
        with open(desktop_src, "r", encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        target_desktop = os.path.join(app_target_dir, f"{app_name}.desktop")
        with open(target_desktop, "w", encoding='utf-8') as f:
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
        logging.warning("No native .desktop file found. Creating a generic one.")
        with open(os.path.join(app_target_dir, f"{app_name}.desktop"), "w") as f:
            f.write(f"[Desktop Entry]\nName={app_name.capitalize()}\nExec={target_bin} %U\nIcon={icon_name}\nType=Application\n")

    icon_search_dirs = [
        os.path.join(staging_root, "usr", "share", "icons"),
        os.path.join(staging_root, "opt")
    ]
    
    for idir in icon_search_dirs:
        if os.path.exists(idir):
            for root, dirs, files in os.walk(idir):
                for file in files:
                    if file.startswith(icon_name) and file.endswith((".png", ".svg")):
                        shutil.copy(os.path.join(root, file), os.path.join(icon_target_dir, file))
                    
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
            logging.info(f"Will download {len(missing_deps)} missing packages:")
            for dep in missing_deps:
                print(f" -> {dep}", flush=True)
                
            logging.info("Starting live DNF download...")
            
            cmd_dnf = ["dnf", "--refresh", "download", "-y", f"--destdir={dnf_dir}"] + missing_deps
            process = subprocess.Popen(cmd_dnf, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                print(line.strip(), flush=True)
            process.wait()
            
            if process.returncode != 0:
                logging.error("DNF download failed.")
                sys.exit(1)

        rpms_to_extract = local_rpms + [os.path.join(dnf_dir, f) for f in os.listdir(dnf_dir) if f.endswith(".rpm")]

        logging.info("Extracting RPMs...")
        for rpm in rpms_to_extract:
            ps = subprocess.Popen(["rpm2cpio", rpm], stdout=subprocess.PIPE)
            subprocess.run(["cpio", "-idmv"], stdin=ps.stdout, cwd=staging_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ps.wait()

        fix_absolute_symlinks(staging_root)
        entrypoint_suffix = detect_entrypoint(staging_root, name)

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
        
        integrate_into_system(name, final_app_path, staging_root)

    finally:
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)
            logging.info("Cleaned up build directory.")

if __name__ == "__main__":
    main()
