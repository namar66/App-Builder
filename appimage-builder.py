#!/usr/bin/env python3

# Modern Standalone App Builder
# Generates a fully isolated, standalone executable with embedded EROFS payload.
# Features: Strict sandbox security, private home, versioning, GPG checks, dynamic binds/masks, Host-Native mode.

import os
import sys
import subprocess
import shutil
import logging
import struct

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def run_cmd(cmd, cwd=None):
    try:
        return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, errors="replace")
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {' '.join(cmd)}\nError: {e.stderr}")
        sys.exit(1)

def verify_rpm_signatures(repo_rpms, local_rpms):
    logging.info("Verifying RPM package signatures for security...")
    if repo_rpms:
        for rpm_path in repo_rpms:
            res = subprocess.run(["rpm", "-K", rpm_path], capture_output=True, text=True, errors="replace")
            if res.returncode != 0 or "NOT OK" in res.stdout or "NOKEY" in res.stdout:
                logging.error(f"SECURITY FATAL: Downloaded package {os.path.basename(rpm_path)} failed GPG verification!")
                sys.exit(1)

    if local_rpms:
        for rpm_path in local_rpms:
            res = subprocess.run(["rpm", "-K", rpm_path], capture_output=True, text=True, errors="replace")
            if res.returncode == 0 and "NOT OK" not in res.stdout and "NOKEY" not in res.stdout and ("pgp" in res.stdout.lower() or "rsa" in res.stdout.lower()):
                continue
            logging.warning(f"WARNING: Local package '{os.path.basename(rpm_path)}' is unsigned or corrupted.")
            if not sys.stdin.isatty():
                logging.error("SECURITY FATAL: Cannot ask for user confirmation in background mode. Aborting.")
                sys.exit(1)
            while True:
                try:
                    choice = input(f"Do you want to proceed with building this untrusted package? [y/N]: ").strip().lower()
                    if choice == 'y': break
                    elif choice in ['n', '']: sys.exit(1)
                except EOFError:
                    sys.exit(1)

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
        if app_name.lower() in os.path.basename(df_path).lower() or len(desktop_files) == 1:
            with open(df_path, 'r', encoding='utf-8', errors='ignore') as df:
                for line in df:
                    if line.startswith("Exec="):
                        cmd = line.strip().split("=", 1)[1]
                        parts = cmd.split()
                        for part in parts:
                            if "=" not in part and part.lower() != "env":
                                desktop_exec = os.path.basename(part).strip("\"'")
                                break
                        if not desktop_exec and len(parts) > 0:
                            desktop_exec = os.path.basename(parts[0]).strip("\"'")
                        break
        if desktop_exec: break

    targets = [desktop_exec] if desktop_exec else []
    targets.extend([app_name, app_name.lower()])

    search_dirs = ["usr/bin", "bin", "usr/sbin", "sbin", "opt", "usr/libexec"]
    for target in targets:
        for sdir in search_dirs:
            full_sdir = os.path.join(staging_root, sdir)
            if os.path.exists(full_sdir):
                for root, dirs, files in os.walk(full_sdir):
                    if target in files:
                        rel_path = os.path.relpath(os.path.join(root, target), staging_root)
                        logging.info(f"Smart-detected entrypoint: /{rel_path}")
                        return "/" + rel_path

    for target in targets:
        for sdir in search_dirs:
            full_sdir = os.path.join(staging_root, sdir)
            if os.path.exists(full_sdir):
                for root, dirs, files in os.walk(full_sdir):
                    for f in files:
                        if f.lower() == target.lower():
                            rel_path = os.path.relpath(os.path.join(root, f), staging_root)
                            logging.info(f"Fuzzy-detected entrypoint: /{rel_path}")
                            return "/" + rel_path

    logging.warning("Could not auto-detect entrypoint. Falling back to bash.")
    return "/usr/bin/bash"

def generate_c_wrapper(source_path, entrypoint_suffix, app_name, app_version, app_mode):
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

typedef struct __attribute__((packed)) {{
    uint64_t erofs_offset;
    uint32_t magic;
}} app_footer_t;

int main(int argc, char *argv[]) {{
    if (argc == 2 && strcmp(argv[1], "--app-version") == 0) {{
        printf("%s\\n", "{app_version}");
        return 0;
    }}
    if (argc == 2 && strcmp(argv[1], "--app-mode") == 0) {{
        printf("%s\\n", "{app_mode}");
        return 0;
    }}

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

    if (footer.magic != 0x41505049) return 1;
    close(fd_self);

    char mount_point[] = "/tmp/appmnt.XXXXXX";
    if (mkdtemp(mount_point) == NULL) return 1;

    char offset_str[64];
    snprintf(offset_str, sizeof(offset_str), "--offset=%llu", (unsigned long long)footer.erofs_offset);

    pid_t fuse_pid = fork();
    if (fuse_pid == 0) {{
        int dev_null = open("/dev/null", O_WRONLY);
        if (dev_null != -1) {{ dup2(dev_null, 1); dup2(dev_null, 2); close(dev_null); }}
        char *fuse_args[] = {{"erofsfuse", offset_str, self_path, mount_point, NULL}};
        execvp(fuse_args[0], fuse_args);
        exit(1);
    }}

    usleep(250000);

    pid_t app_pid = fork();
    if (app_pid == 0) {{
        char path_env[2048], ld_env[2048], xdg_env[2048], entrypoint_path[2048];
        char opt_bind_src[2048], libgl_env[2048], private_home[2048], mkdir_cmd[2048];

        snprintf(path_env, sizeof(path_env), "/opt/approot/usr/bin:/usr/bin:/bin");
        snprintf(ld_env, sizeof(ld_env), "/opt/approot/usr/lib64:/lib:/lib/pulseaudio:/lib/alsa-lib:/usr/lib64");
        snprintf(xdg_env, sizeof(xdg_env), "/opt/approot/usr/share:/usr/share");
        snprintf(entrypoint_path, sizeof(entrypoint_path), "/opt/approot%s", "{entrypoint_suffix}");
        snprintf(opt_bind_src, sizeof(opt_bind_src), "%s/opt", mount_point);
        snprintf(libgl_env, sizeof(libgl_env), "/opt/approot/usr/lib64/dri:/lib/dri:/usr/lib64/dri");

        char *home = getenv("HOME");
        if (!home) home = "/";

        snprintf(private_home, sizeof(private_home), "%s/.var/app/%s", home, "{app_name}");
        snprintf(mkdir_cmd, sizeof(mkdir_cmd), "mkdir -p %s", private_home);
        system(mkdir_cmd);

        char *b_args[1024]; int arg_idx = 0;
        b_args[arg_idx++] = "bwrap";
        b_args[arg_idx++] = "--ro-bind"; b_args[arg_idx++] = "/usr"; b_args[arg_idx++] = "/usr";
        b_args[arg_idx++] = "--ro-bind"; b_args[arg_idx++] = "/etc"; b_args[arg_idx++] = "/etc";
        b_args[arg_idx++] = "--dev-bind"; b_args[arg_idx++] = "/dev"; b_args[arg_idx++] = "/dev";
        b_args[arg_idx++] = "--proc"; b_args[arg_idx++] = "/proc";
        b_args[arg_idx++] = "--ro-bind"; b_args[arg_idx++] = "/sys"; b_args[arg_idx++] = "/sys";
        b_args[arg_idx++] = "--bind"; b_args[arg_idx++] = "/tmp"; b_args[arg_idx++] = "/tmp";
        b_args[arg_idx++] = "--dir"; b_args[arg_idx++] = "/run";
        b_args[arg_idx++] = "--bind-try"; b_args[arg_idx++] = "/run/user"; b_args[arg_idx++] = "/run/user";
        b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = "/run/dbus"; b_args[arg_idx++] = "/run/dbus";

        b_args[arg_idx++] = "--bind"; b_args[arg_idx++] = private_home; b_args[arg_idx++] = home;

        char host_icons[2048], host_local_icons[2048], kde_config[2048], font_config[2048];
        snprintf(host_icons, sizeof(host_icons), "%s/.icons", home);
        snprintf(host_local_icons, sizeof(host_local_icons), "%s/.local/share/icons", home);
        snprintf(kde_config, sizeof(kde_config), "%s/.config/kdeglobals", home);
        snprintf(font_config, sizeof(font_config), "%s/.config/fontconfig", home);

        b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = host_icons; b_args[arg_idx++] = host_icons;
        b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = host_local_icons; b_args[arg_idx++] = host_local_icons;
        b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = kde_config; b_args[arg_idx++] = kde_config;
        b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = font_config; b_args[arg_idx++] = font_config;

        char binds_file[2048]; snprintf(binds_file, sizeof(binds_file), "%s/binds.txt", private_home);
        FILE *touch_bf = fopen(binds_file, "a"); if(touch_bf) fclose(touch_bf);
        FILE *bf = fopen(binds_file, "r");
        if (bf) {{
            char line[2048];
            while (fgets(line, sizeof(line), bf)) {{
                line[strcspn(line, "\\n")] = 0;
                if (strlen(line) > 0) {{ b_args[arg_idx++] = "--bind-try"; b_args[arg_idx++] = strdup(line); b_args[arg_idx++] = strdup(line); }}
            }}
            fclose(bf);
        }}

        char masks_file[2048]; snprintf(masks_file, sizeof(masks_file), "%s/masks.txt", private_home);
        FILE *touch_mf = fopen(masks_file, "a"); if(touch_mf) fclose(touch_mf);
        FILE *mf = fopen(masks_file, "r");
        if (mf) {{
            char line[2048];
            while (fgets(line, sizeof(line), mf)) {{
                line[strcspn(line, "\\n")] = 0;
                if (strlen(line) > 0) {{ b_args[arg_idx++] = "--tmpfs"; b_args[arg_idx++] = strdup(line); }}
            }}
            fclose(mf);
        }}

        const char *sensitive_paths[] = {{".bashrc", ".bash_profile", ".bash_logout", ".profile", ".zshrc", ".ssh", ".config/autostart"}};
        for (int j = 0; j < 7; j++) {{
            char sp1[2048], sp2[2048];
            snprintf(sp1, sizeof(sp1), "%s/%s", home, sensitive_paths[j]);
            b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = strdup(sp1); b_args[arg_idx++] = strdup(sp1);
            snprintf(sp2, sizeof(sp2), "%s/%s", private_home, sensitive_paths[j]);
            b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = strdup(sp2); b_args[arg_idx++] = strdup(sp2);
        }}

        b_args[arg_idx++] = "--tmpfs"; b_args[arg_idx++] = "/etc/ssl";
        b_args[arg_idx++] = "--dir"; b_args[arg_idx++] = "/etc/ssl/certs";
        b_args[arg_idx++] = "--symlink"; b_args[arg_idx++] = "/etc/pki/tls/certs/ca-bundle.crt"; b_args[arg_idx++] = "/etc/ssl/certs/ca-certificates.crt";
        b_args[arg_idx++] = "--symlink"; b_args[arg_idx++] = "/etc/pki/tls/certs/ca-bundle.crt"; b_args[arg_idx++] = "/etc/ssl/certs/ca-bundle.crt";

        b_args[arg_idx++] = "--symlink"; b_args[arg_idx++] = "usr/bin"; b_args[arg_idx++] = "/bin";
        b_args[arg_idx++] = "--symlink"; b_args[arg_idx++] = "usr/sbin"; b_args[arg_idx++] = "/sbin";
        b_args[arg_idx++] = "--symlink"; b_args[arg_idx++] = "usr/lib64"; b_args[arg_idx++] = "/lib64";

        b_args[arg_idx++] = "--ro-bind"; b_args[arg_idx++] = mount_point; b_args[arg_idx++] = "/opt/approot";
        b_args[arg_idx++] = "--dir"; b_args[arg_idx++] = "/opt";
        b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = opt_bind_src; b_args[arg_idx++] = "/opt/approot/opt";

        char app_lib[2048]; snprintf(app_lib, sizeof(app_lib), "%s/usr/lib", mount_point);
        b_args[arg_idx++] = "--ro-bind-try"; b_args[arg_idx++] = app_lib; b_args[arg_idx++] = "/lib";

        b_args[arg_idx++] = "--setenv"; b_args[arg_idx++] = "PATH"; b_args[arg_idx++] = path_env;
        b_args[arg_idx++] = "--setenv"; b_args[arg_idx++] = "LD_LIBRARY_PATH"; b_args[arg_idx++] = ld_env;
        b_args[arg_idx++] = "--setenv"; b_args[arg_idx++] = "XDG_DATA_DIRS"; b_args[arg_idx++] = xdg_env;
        b_args[arg_idx++] = "--setenv"; b_args[arg_idx++] = "LIBGL_DRIVERS_PATH"; b_args[arg_idx++] = libgl_env;
        b_args[arg_idx++] = "--setenv"; b_args[arg_idx++] = "GTK_USE_PORTAL"; b_args[arg_idx++] = "1";
        b_args[arg_idx++] = "--setenv"; b_args[arg_idx++] = "SSL_CERT_DIR"; b_args[arg_idx++] = "/etc/pki/tls/certs";
        b_args[arg_idx++] = "--setenv"; b_args[arg_idx++] = "SSL_CERT_FILE"; b_args[arg_idx++] = "/etc/pki/tls/certs/ca-bundle.crt";

        b_args[arg_idx++] = entrypoint_path;
        if (strstr(entrypoint_path, "steam") != NULL) b_args[arg_idx++] = "-no-cef-sandbox";

        for (int i = 1; i < argc && arg_idx < 1023; i++) b_args[arg_idx++] = argv[i];
        b_args[arg_idx] = NULL;

        execvp(b_args[0], b_args);
        exit(1);
    }}

    int status;
    waitpid(app_pid, &status, 0);

    pid_t umount_pid = fork();
    if (umount_pid == 0) {{
        int dev_null = open("/dev/null", O_WRONLY);
        if (dev_null != -1) {{ dup2(dev_null, 1); dup2(dev_null, 2); close(dev_null); }}
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
    search_desktop_dirs = [os.path.join(staging_root, "usr", "share", "applications"), os.path.join(staging_root, "opt")]

    for sdir in search_desktop_dirs:
        if os.path.exists(sdir):
            for root, dirs, files in os.walk(sdir):
                for f in files:
                    if f.endswith(".desktop"):
                        desktop_src = os.path.join(root, f)
                        if app_name in f: break
                if desktop_src: break
        if desktop_src: break

    if desktop_src:
        with open(desktop_src, "r", encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        target_desktop = os.path.join(app_target_dir, f"{app_name}.desktop")
        with open(target_desktop, "w", encoding='utf-8') as f:
            for line in lines:
                if line.startswith("Exec="): f.write(f"Exec={target_bin} %U\n")
                elif line.startswith("TryExec="): continue
                else:
                    if line.startswith("Icon="): icon_name = line.split("=")[1].strip()
                    f.write(line)
    else:
        logging.warning("No native .desktop file found. Creating generic one.")
        with open(os.path.join(app_target_dir, f"{app_name}.desktop"), "w") as f:
            f.write(f"[Desktop Entry]\nName={app_name.capitalize()}\nExec={target_bin} %U\nIcon={icon_name}\nType=Application\n")

    icon_search_dirs = [os.path.join(staging_root, "usr", "share", "icons"), os.path.join(staging_root, "opt")]
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
    logging.info(f"Successfully integrated {app_name}. Menu is now updated.")

def main():
    if len(sys.argv) < 3:
        print("Usage: appimage-builder.py <App-Name> <Portable|Host-Native> [Package1 ...]")
        sys.exit(1)

    name = sys.argv[1]
    app_mode = sys.argv[2]
    requested_packages = sys.argv[3:] if len(sys.argv) > 3 else [name]

    output_dir = os.path.abspath("./")
    build_dir = f"/var/tmp/app-build-{name}"
    staging_root = os.path.join(build_dir, "root")

    try:
        if os.path.exists(build_dir):
            subprocess.run(["chmod", "-R", "u+rwX", build_dir], capture_output=True)
            subprocess.run(["rm", "-rf", build_dir])

        os.makedirs(staging_root, exist_ok=True)

        local_rpms = [p for p in requested_packages if p.endswith(".rpm") and os.path.isfile(p)]
        repo_packages = [p for p in requested_packages if p not in local_rpms]

        dnf_dir = os.path.join(build_dir, "dnf-downloads")
        os.makedirs(dnf_dir, exist_ok=True)

        if repo_packages:
            needs_multilib = any("steam" in p.lower() or "wine" in p.lower() or ".i686" in p.lower() for p in repo_packages)

            arch_filter = "--exclude=*.src.*,*-debugsource-*,*-debuginfo-*" if needs_multilib else "--exclude=*.i686,*.src.*,*-debugsource-*,*-debuginfo-*"

            logging.info(f"Resolving dependencies for {app_mode} mode ({arch_filter})...")

            if app_mode == "Portable":
                cmd_dnf = ["dnf", "download", "--resolve", "-y", "--alldeps", arch_filter, f"--destdir={dnf_dir}"] + repo_packages
            else:
                cmd_dnf = ["dnf", "download", "-y", arch_filter, f"--destdir={dnf_dir}"] + repo_packages

            process = subprocess.Popen(cmd_dnf, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            for line in process.stdout: print(line.strip(), flush=True)
            process.wait()

            if process.returncode != 0:
                logging.error("DNF download failed. Check package names.")
                sys.exit(1)

        rpms_from_repo = [os.path.join(dnf_dir, f) for f in os.listdir(dnf_dir) if f.endswith(".rpm")]
        verify_rpm_signatures(rpms_from_repo, local_rpms)
        rpms_to_extract = local_rpms + rpms_from_repo

        app_version = "unknown"
        logging.info("Detecting package version...")
        for rpm in rpms_to_extract:
            if name.lower() in os.path.basename(rpm).lower():
                res = subprocess.run(["rpm", "-qp", "--queryformat", "%{VERSION}-%{RELEASE}", rpm], capture_output=True, text=True, errors="replace")
                if res.returncode == 0:
                    app_version = res.stdout.strip()
                    break

        logging.info("Extracting RPMs...")
        for rpm in rpms_to_extract:
            ps = subprocess.Popen(["rpm2cpio", rpm], stdout=subprocess.PIPE)
            subprocess.run(["cpio", "-idmu"], stdin=ps.stdout, cwd=staging_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ps.wait()
            subprocess.run(["chmod", "-R", "u+rwX", staging_root])

        logging.info("Applying UsrMerge filesystem fixes...")
        for d in ["lib", "lib64", "bin", "sbin"]:
            src = os.path.join(staging_root, d)
            dst = os.path.join(staging_root, "usr", d)
            if os.path.exists(src) and not os.path.islink(src):
                os.makedirs(dst, exist_ok=True)
                os.system(f"cp -rn '{src}'/* '{dst}'/ 2>/dev/null || true")
                os.system(f"rm -rf '{src}'")
                os.symlink(f"usr/{d}", src)

        logging.info("Normalizing filesystem permissions for packaging...")
        subprocess.run(["chmod", "-R", "u+rwX", staging_root])

        fix_absolute_symlinks(staging_root)
        entrypoint_suffix = detect_entrypoint(staging_root, name)

        erofs_payload = os.path.join(build_dir, "payload.erofs")
        selinux_contexts = "/run/host/etc/selinux/targeted/contexts/files/file_contexts"

        cmd_erofs = ["mkfs.erofs", "-x1", "--all-root", "-U", "clear", "-T", "0"]

        if os.path.exists(selinux_contexts):
            logging.info(f"Applying SELinux contexts from {selinux_contexts}...")
            cmd_erofs.append(f"--file-contexts={selinux_contexts}")
        else:
            logging.warning("SELinux contexts not found on host. Image will lack proper SELinux labels.")

        cmd_erofs.extend([erofs_payload, staging_root])

        logging.info("Building EROFS filesystem...")
        run_cmd(cmd_erofs)

        c_source_path = os.path.join(build_dir, "wrapper.c")
        runtime_bin_path = os.path.join(build_dir, "runtime")
        generate_c_wrapper(c_source_path, entrypoint_suffix, name, app_version, app_mode)
        run_cmd(["gcc", "-O2", c_source_path, "-o", runtime_bin_path])

        final_app_path = os.path.join(output_dir, f"{name}.app")
        stitch_app(final_app_path, runtime_bin_path, erofs_payload)
        integrate_into_system(name, final_app_path, staging_root)

    finally:
        if os.path.exists(build_dir):
            subprocess.run(["chmod", "-R", "u+rwX", build_dir], capture_output=True)
            subprocess.run(["rm", "-rf", build_dir])
            logging.info("Cleaned up build directory.")

if __name__ == "__main__":
    main()
