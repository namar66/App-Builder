#!/usr/bin/python3

import sys
import os
import subprocess
import logging
import time

# Configure logging to show up nicely in journalctl
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def safe_run_command(cmd_list, timeout_sec=120):
    """Executes a command securely with a strict timeout. Returns stdout or None on failure."""
    try:
        # We enforce a hard timeout on all subprocess calls to prevent infinite hangs
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=True,
            errors="replace"
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logging.error(f"Command '{' '.join(cmd_list[:3])}...' timed out after {timeout_sec}s. Killing it.")
        return None
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed with exit code {e.returncode}: {e.stderr.strip()}")
        return None
    except Exception as e:
        logging.error(f"Unexpected execution error: {str(e)}")
        return None

def check_and_update():
    bin_dir = os.path.expanduser("~/.local/bin")
    if not os.path.exists(bin_dir):
        logging.info("Bin directory not found. Nothing to update today.")
        return

    # Strip display variables so GUI apps don't accidentally launch during version checks
    safe_env = os.environ.copy()
    safe_env.pop("DISPLAY", None)
    safe_env.pop("WAYLAND_DISPLAY", None)

    installed_apps = {}
    logging.info("Scanning for installed .app containers...")

    for f in os.listdir(bin_dir):
        if f.endswith(".app"):
            app_name = f[:-4]
            app_path = os.path.join(bin_dir, f)

            # Extract current version and mode with a very short 5-second timeout
            try:
                res_ver = subprocess.run([app_path, "--app-version"], capture_output=True, text=True, timeout=5, env=safe_env, errors="replace")
                res_mod = subprocess.run([app_path, "--app-mode"], capture_output=True, text=True, timeout=5, env=safe_env, errors="replace")

                if res_ver.returncode == 0 and res_ver.stdout.strip():
                    installed_apps[app_name] = {
                        "ver": res_ver.stdout.strip(),
                        "mode": res_mod.stdout.strip() if res_mod.returncode == 0 else "Portable"
                    }
                else:
                    logging.warning(f"Could not cleanly read version for {app_name}. Skipping.")
            except Exception as e:
                logging.warning(f"Failed to inspect {app_name}: {e}")

    if not installed_apps:
        logging.info("No valid applications found. Exiting.")
        return

    logging.info(f"Found {len(installed_apps)} applications. Querying Flathub/DNF for updates...")

    # Check updates via toolbox DNF (Network call -> strict 60s timeout!)
    cmd_repoquery = [
        "toolbox", "run", "-c", "sysext-builder", "env", "LANG=C",
        "dnf", "repoquery", "--latest-limit", "1", "--quiet",
        "--queryformat", "%{name}|%{version}-%{release}"
    ] + list(installed_apps.keys())

    repo_out = safe_run_command(cmd_repoquery, timeout_sec=60)

    if repo_out is None:
        logging.error("Failed to fetch repository metadata. Network might be down. Aborting update cycle.")
        return

    latest_versions = {}
    for line in repo_out.splitlines():
        if "|" in line:
            name, ver = line.split("|", 1)
            latest_versions[name] = ver

    builder_script = os.path.join(bin_dir, "appimage-builder.py")
    if not os.path.exists(builder_script):
        logging.error(f"Builder script missing at {builder_script}. Cannot perform updates.")
        return

    # Process updates
    for app_name, data in installed_apps.items():
        current_ver = data["ver"]
        latest_ver = latest_versions.get(app_name)

        if latest_ver and current_ver != latest_ver and current_ver != "unknown":
            logging.info(f"🚀 Update available for {app_name}: {current_ver} -> {latest_ver}. Starting build...")

            # Rebuild command (Heavy IO/CPU task -> generous 600s timeout)
            build_cmd = ["toolbox", "run", "-c", "sysext-builder", "env", "LANG=C", builder_script, app_name, data["mode"], app_name]
            build_out = safe_run_command(build_cmd, timeout_sec=600)

            if build_out is not None:
                logging.info(f"✅ Successfully updated {app_name} to version {latest_ver}.")
            else:
                logging.error(f"❌ Failed to build update for {app_name}. Kept the old version.")
        else:
            logging.debug(f"{app_name} is already up to date ({current_ver}).")

if __name__ == "__main__":
    logging.info("=== Starting App Auto-Updater Cycle ===")
    check_and_update()
    logging.info("=== Update Cycle Finished ===")
