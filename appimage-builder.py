#!/usr/bin/env python3

# Modern Standalone App Builder
# Generates a fully isolated, standalone executable with embedded EROFS payload.
# Features: Strict sandbox security, private home, versioning, GPG checks, dynamic binds/masks, Host-Native mode.

import struct
import sys
import os
import shutil
import subprocess
import logging

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
    logging.info(f"Generating advanced FUSE C wrapper (SteamWebHelper fix) for {app_name}...")

    wrapper_c_path = os.path.join(source_path, f"{app_name}_wrapper.c")
    wrapper_bin_path = os.path.join(source_path, app_name)

    c_code = f"""
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>
    #include <unistd.h>
    #include <limits.h>
    #include <pwd.h>
    #include <sys/types.h>
    #include <sys/wait.h>
    #include <stdint.h>
    #include <sys/stat.h>

    #define MAX_ARGS 2048

    int main(int argc, char *argv[]) {{
        char *args[MAX_ARGS];
        int arg_count = 0;

        void add_arg(const char *arg) {{
            if (arg_count < MAX_ARGS - 1) {{
                args[arg_count++] = strdup(arg);
            }}
        }}

        // 1. Zjistíme naši cestu
        char app_path[PATH_MAX];
        ssize_t len = readlink("/proc/self/exe", app_path, sizeof(app_path)-1);
        if (len == -1) {{
            perror("NYX ERROR: readlink failed"); return 1;
        }}
        app_path[len] = '\\0';

        // 2. Extrakce EROFS offsetu
        uint64_t erofs_offset = 0;
        uint32_t magic = 0;
        FILE *f = fopen(app_path, "rb");
        if (f) {{
            fseek(f, -12, SEEK_END);
            if (fread(&erofs_offset, 1, 8, f) != 8) {{ fclose(f); return 1; }}
            if (fread(&magic, 1, 4, f) != 4) {{ fclose(f); return 1; }}
            fclose(f);
        }}

        if (magic != 0x41505049) {{
            fprintf(stderr, "NYX FATAL: Magic footer not found. Payload is missing!\\n");
            return 1;
        }}

        // 3. FUSE mountpoint v RAM
        char mount_dir[] = "/tmp/.nyx_app_XXXXXX";
        if (!mkdtemp(mount_dir)) {{
            perror("NYX ERROR: mkdtemp failed"); return 1;
        }}

        char offset_str[64];
        snprintf(offset_str, sizeof(offset_str), "--offset=%llu", (unsigned long long)erofs_offset);

        pid_t fuse_pid = fork();
        if (fuse_pid == 0) {{
            execlp("erofsfuse", "erofsfuse", offset_str, app_path, mount_dir, NULL);
            fprintf(stderr, "NYX FATAL: erofsfuse failed to execute.\\n");
            exit(1);
        }}

        int fuse_status;
        waitpid(fuse_pid, &fuse_status, 0);
        if (WEXITSTATUS(fuse_status) != 0) {{
            fprintf(stderr, "NYX FATAL: Failed to mount EROFS payload.\\n");
            rmdir(mount_dir);
            return 1;
        }}
        usleep(150000);

        // 4. Domovské adresáře a Private Home
        char home_dir[PATH_MAX];
        struct passwd *pw = getpwuid(getuid());
        if (pw) {{
            strncpy(home_dir, pw->pw_dir, sizeof(home_dir) - 1);
        }} else {{
            strncpy(home_dir, getenv("HOME"), sizeof(home_dir) - 1);
        }}

        char app_dir[PATH_MAX];
        snprintf(app_dir, sizeof(app_dir), "%s/.var/app/{app_name}", home_dir);
        char private_home[PATH_MAX];
        snprintf(private_home, sizeof(private_home), "%s/home", app_dir);

        char mkdir_cmd[PATH_MAX + 128];
        snprintf(mkdir_cmd, sizeof(mkdir_cmd), "mkdir -p \\"%s\\"", private_home);
        system(mkdir_cmd);

        char metadata_path[PATH_MAX];
        snprintf(metadata_path, sizeof(metadata_path), "%s/metadata", app_dir);

        int no_sandbox = 0;
        FILE *meta_check = fopen(metadata_path, "r");
        if (meta_check) {{
            char line[1024];
            while (fgets(line, sizeof(line), meta_check)) {{
                if (strstr(line, "nosandbox=true")) no_sandbox = 1;
            }}
            fclose(meta_check);
        }}

        // 5. Build bwrap argumentů
        add_arg("bwrap");

        if (no_sandbox) {{
            add_arg("--bind"); add_arg("/"); add_arg("/");
            add_arg("--dev-bind"); add_arg("/dev"); add_arg("/dev");
            add_arg("--proc"); add_arg("/proc");
            add_arg("--bind-try"); add_arg("/sys"); add_arg("/sys");
        }} else {{
            add_arg("--bind"); add_arg(private_home); add_arg(home_dir);

            if (strncmp(home_dir, "/var/home", 9) == 0) {{
                add_arg("--symlink"); add_arg("var/home"); add_arg("/home");
            }}

            add_arg("--tmpfs"); add_arg("/tmp");
            add_arg("--bind-try"); add_arg("/tmp/.X11-unix"); add_arg("/tmp/.X11-unix");
            add_arg("--bind-try"); add_arg("/var/tmp"); add_arg("/var/tmp");
            add_arg("--bind-try"); add_arg("/dev/shm"); add_arg("/dev/shm");

            add_arg("--ro-bind"); add_arg("/usr"); add_arg("/usr");

            // Kinoite 32-bit fix
            char payload_usr_lib[PATH_MAX];
            snprintf(payload_usr_lib, sizeof(payload_usr_lib), "%s/usr/lib", mount_dir);
            add_arg("--ro-bind-try"); add_arg(payload_usr_lib); add_arg("/usr/lib");

            add_arg("--symlink"); add_arg("usr/lib"); add_arg("/lib");
            add_arg("--symlink"); add_arg("usr/lib64"); add_arg("/lib64");
            add_arg("--symlink"); add_arg("usr/bin"); add_arg("/bin");
            add_arg("--symlink"); add_arg("usr/sbin"); add_arg("/sbin");

            add_arg("--dev-bind"); add_arg("/dev"); add_arg("/dev");
            add_arg("--proc"); add_arg("/proc");
            add_arg("--ro-bind-try"); add_arg("/sys"); add_arg("/sys");

            add_arg("--ro-bind-try"); add_arg("/etc/os-release"); add_arg("/etc/os-release");
            add_arg("--ro-bind-try"); add_arg("/etc/resolv.conf"); add_arg("/etc/resolv.conf");
            add_arg("--ro-bind-try"); add_arg("/etc/machine-id"); add_arg("/etc/machine-id");
            add_arg("--ro-bind-try"); add_arg("/etc/passwd"); add_arg("/etc/passwd");
            add_arg("--ro-bind-try"); add_arg("/etc/group"); add_arg("/etc/group");

            // --- PRESSURE VESSEL / STEAMWEBHELPER FIX ---
            add_arg("--ro-bind-try"); add_arg("/etc/ld.so.cache"); add_arg("/etc/ld.so.cache");
            add_arg("--dir"); add_arg("/var/cache/ldconfig"); // Vyrobíme Ubuntu složku
            add_arg("--symlink"); add_arg("/etc/ld.so.cache"); add_arg("/var/cache/ldconfig/ld.so.cache"); // Lžeme Steamu
            // --------------------------------------------

            add_arg("--ro-bind-try"); add_arg("/etc/ssl"); add_arg("/etc/ssl");
            add_arg("--ro-bind-try"); add_arg("/etc/pki"); add_arg("/etc/pki");
            add_arg("--ro-bind-try"); add_arg("/usr/share/ca-certificates"); add_arg("/usr/share/ca-certificates");

            add_arg("--ro-bind-try"); add_arg("/etc/fonts"); add_arg("/etc/fonts");
            add_arg("--ro-bind-try"); add_arg("/usr/share/fonts"); add_arg("/usr/share/fonts");
            add_arg("--ro-bind-try"); add_arg("/usr/share/icons"); add_arg("/usr/share/icons");
            add_arg("--ro-bind-try"); add_arg("/usr/share/themes"); add_arg("/usr/share/themes");
            add_arg("--ro-bind-try"); add_arg("/usr/share/i18n"); add_arg("/usr/share/i18n");
        }}

        // Aplikaci izolujeme do /nyx
        add_arg("--ro-bind"); add_arg(mount_dir); add_arg("/nyx");

        add_arg("--setenv"); add_arg("PATH"); add_arg("/nyx/usr/bin:/nyx/usr/sbin:/usr/bin:/usr/sbin");
        add_arg("--setenv"); add_arg("LD_LIBRARY_PATH"); add_arg("/nyx/usr/lib:/nyx/usr/lib64:/usr/lib64:/lib64");
        add_arg("--setenv"); add_arg("XDG_DATA_DIRS"); add_arg("/nyx/usr/share:/usr/share");

        if (getenv("WAYLAND_DISPLAY")) {{ add_arg("--setenv"); add_arg("WAYLAND_DISPLAY"); add_arg(getenv("WAYLAND_DISPLAY")); }}
        if (getenv("DISPLAY")) {{ add_arg("--setenv"); add_arg("DISPLAY"); add_arg(getenv("DISPLAY")); }}
        if (getenv("XDG_RUNTIME_DIR")) {{
            add_arg("--setenv"); add_arg("XDG_RUNTIME_DIR"); add_arg(getenv("XDG_RUNTIME_DIR"));
            if (!no_sandbox) {{ add_arg("--bind-try"); add_arg(getenv("XDG_RUNTIME_DIR")); add_arg(getenv("XDG_RUNTIME_DIR")); }}
        }}

        // Metadata Binds
        FILE *meta_file = fopen(metadata_path, "r");
        if (meta_file) {{
            char line[1024];
            char current_section[64] = "";
            while (fgets(line, sizeof(line), meta_file)) {{
                line[strcspn(line, "\\r\\n")] = 0;
                if (line[0] == '[') {{ sscanf(line, "[%63[^]]]", current_section); continue; }}

                char *eq = strchr(line, '=');
                if (!eq) continue;
                *eq = '\\0'; char *key = line; char *val = eq + 1;

                if (strcmp(current_section, "Context") == 0 && strstr(key, "filesystems")) {{
                    char *token = strtok(val, ";");
                    while (token) {{
                        if (strlen(token) > 0) {{
                            char resolved[PATH_MAX];
                            if (strncmp(token, "home", 4) == 0) snprintf(resolved, PATH_MAX, "%s", home_dir);
                            else if (strncmp(token, "xdg-config", 10) == 0) snprintf(resolved, PATH_MAX, "%s/.config%s", home_dir, token+10);
                            else if (strncmp(token, "xdg-data", 8) == 0) snprintf(resolved, PATH_MAX, "%s/.local/share%s", home_dir, token+8);
                            else if (strncmp(token, "xdg-download", 12) == 0) snprintf(resolved, PATH_MAX, "%s/Downloads%s", home_dir, token+12);
                            else if (strncmp(token, "~/", 2) == 0) snprintf(resolved, PATH_MAX, "%s/%s", home_dir, token+2);
                            else strncpy(resolved, token, PATH_MAX - 1);

                            char *suffix = strchr(resolved, ':');
                            if (suffix) *suffix = '\\0';

                            add_arg("--bind-try"); add_arg(resolved); add_arg(resolved);
                        }}
                        token = strtok(NULL, ";");
                    }}
                }}
                else if (strcmp(current_section, "Context") == 0 && strstr(key, "sockets")) {{
                    if (strstr(val, "wayland")) {{
                        char wl[PATH_MAX]; snprintf(wl, PATH_MAX, "/run/user/%d/%s", getuid(), getenv("WAYLAND_DISPLAY") ? getenv("WAYLAND_DISPLAY") : "wayland-0");
                        add_arg("--ro-bind-try"); add_arg(wl); add_arg(wl);
                    }}
                    if (strstr(val, "x11") || strstr(val, "fallback-x11")) {{ add_arg("--ro-bind-try"); add_arg("/tmp/.X11-unix"); add_arg("/tmp/.X11-unix"); }}
                    if (strstr(val, "pulseaudio")) {{
                        char pa[PATH_MAX]; snprintf(pa, PATH_MAX, "/run/user/%d/pulse", getuid());
                        add_arg("--ro-bind-try"); add_arg(pa); add_arg(pa);
                    }}
                }}
                else if (strcmp(current_section, "Context") == 0 && strstr(key, "devices")) {{
                    if (strstr(val, "dri")) {{ add_arg("--dev-bind"); add_arg("/dev/dri"); add_arg("/dev/dri"); }}
                }}
            }}
            fclose(meta_file);
        }}

        char entrypoint[PATH_MAX];
        snprintf(entrypoint, PATH_MAX, "/nyx%s", "{entrypoint_suffix}");
        add_arg(entrypoint);

        for (int i = 1; i < argc; i++) add_arg(argv[i]);
        args[arg_count] = NULL;

        pid_t bwrap_pid = fork();
        if (bwrap_pid == 0) {{
            execvp("bwrap", args);
            perror("NYX FATAL: execvp bwrap failed");
            exit(1);
        }}

        int status;
        waitpid(bwrap_pid, &status, 0);

        pid_t umount_pid = fork();
        if (umount_pid == 0) {{
            execlp("fusermount3", "fusermount3", "-u", "-z", mount_dir, NULL);
            execlp("fusermount", "fusermount", "-u", "-z", mount_dir, NULL);
            execlp("umount", "umount", mount_dir, NULL);
            exit(1);
        }}
        waitpid(umount_pid, NULL, 0);
        rmdir(mount_dir);

        return WEXITSTATUS(status);
    }}
    """

    try:
        with open(wrapper_c_path, "w") as f:
            f.write(c_code)
        subprocess.run(["gcc", "-O3", "-s", "-o", wrapper_bin_path, wrapper_c_path], check=True)
        os.remove(wrapper_c_path)
        return wrapper_bin_path
    except Exception as e:
        logging.error(f"Error compiling C wrapper: {e}")
        raise

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

        deps_list = []
        logging.info("Generating dependency manifest...")
        for rpm in rpms_to_extract:
            res = subprocess.run(["rpm", "-qp", "--queryformat", "%{NAME}-%{VERSION}-%{RELEASE}", rpm], capture_output=True, text=True, errors="replace")
            if res.returncode == 0:
                deps_list.append(res.stdout.strip())

        app_private_home = os.path.expanduser(f"~/.var/app/{name}")
        os.makedirs(app_private_home, exist_ok=True)
        with open(os.path.join(app_private_home, "deps.txt"), "w") as f:
            f.write("\n".join(sorted(deps_list)))

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

        # Let the generator do its job and catch the returned binary path directly
        runtime_bin_path = generate_c_wrapper(build_dir, entrypoint_suffix, name, app_version, app_mode)

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
