import os
import json
import shutil
import subprocess
import requests
import zipfile
import datetime
import sys
import time
import traceback
import stat
from check import check_update

# File paths, constants, and custom module imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_VARS_PATH = os.path.join(SCRIPT_DIR, "updateVars.json")

# Load update variables
if not os.path.exists(UPDATE_VARS_PATH):
    raise FileNotFoundError(f"{UPDATE_VARS_PATH} not found.")

with open(UPDATE_VARS_PATH, "r") as f:
    update_vars = json.load(f)

# Determine the root directory based on the PROJECT_ROOT_FROM_OTA_SCRIPT_LOCATION value
root_dir_depth = update_vars.get("PROJECT_ROOT_FROM_OTA_SCRIPT_LOCATION", 0)
ROOT_DIR = SCRIPT_DIR
for _ in range(root_dir_depth):
    ROOT_DIR = os.path.dirname(ROOT_DIR)

LOGS_DIR = os.path.join(ROOT_DIR, "ota_logs")
TEMP_DIR = os.path.join(ROOT_DIR, "temp_update")

# Ensure the logs and temp directories exist
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

TEMP_ZIP_PATH = os.path.join(TEMP_DIR, "update.zip")
WHITELISTED_FILES_KEY = "WHITELISTED_FILES_FOLDERS"


# Version helpers to compare stable/prerelease correctly
def _parse_version(ver: str):
    if not ver:
        return (0, 0, 0, 1, 0)
    ver = ver.strip()
    import re

    m = re.match(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([A-Za-z]+)?(\d+)?)?\s*$", ver)
    if not m:
        nums = [int(x) for x in re.findall(r"\d+", ver)]
        major = nums[0] if len(nums) > 0 else 0
        minor = nums[1] if len(nums) > 1 else 0
        patch = nums[2] if len(nums) > 2 else 0
        return (major, minor, patch, 1, 0)
    major = int(m.group(1) or 0)
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    pre_label = (m.group(4) or "").lower()
    pre_num = int(m.group(5) or 0)
    is_prerelease = 1 if pre_label else 0
    return (major, minor, patch, is_prerelease, pre_num)


def _cmp_versions(a: str, b: str) -> int:
    ta = _parse_version(a or "0")
    tb = _parse_version(b or "0")
    return (ta > tb) - (ta < tb)


def log_error(stage, message, exception=None):
    """
    Logs errors with stage information to the logs directory.
    """
    os.makedirs(LOGS_DIR, exist_ok=True)
    now = datetime.datetime.now().strftime("%d_%m_%Y_%H_%M")
    log_path = os.path.join(LOGS_DIR, f"log_{now}.txt")
    with open(log_path, "a") as log_file:
        log_file.write(f"[{stage}] {message}\n")
        if stage != "OTA Update" and exception:
            log_file.write("[Stack Trace]\n")
            log_file.write(traceback.format_exc() + "\n")


def print_progress(stage, message):
    print(f"[{stage}] {message}")
    log_error(stage, message)


def logo_message(message):
    print("   ____ _______          _____  _____ _____  _____ _____ _______ ")
    print("  / __ \__   __|/\      / ____|/ ____|  __ \|_   _|  __ \__   __|")
    print(" | |  | | | |  /  \    | (___ | |    | |__) | | | | |__) | | |   ")
    print(" | |  | | | | / /\ \    \___ \| |    |  _  /  | | |  _  /  | |   ")
    print(" | |__| | | |/ ____ \   ____) | |____| | \ \ _| |_| | \ \  | |   ")
    print("  \____/  |_/_/    \_\ |_____/ \_____|_|  \_\_____|_|  \_\ |_|   ")
    print("                                                                ")
    print(f"<<<---{message}--->>>")


def smart_download_check():
    # Smart download check to determine whether the file needs to be redownloaded or not or if the bot is up to date or not so that it will not perform unnecessary updates.
    print_progress("Smart Download Check", "Starting smart download check...")
    try:
        # Fetch current and latest version info
        result = check_update()
        if result["status"] == "error":
            log_error("Smart Download Check", result["message"])
            return None

        current_version = result.get("current_version")
        latest_version = result.get("stable_version") or result.get("latest_version")
        prerelease_version = result.get("prerelease_version")

        # Prevent downgrades: if current > latest stable and no newer prerelease exists, abort
        try:
            has_newer_pre = bool(
                prerelease_version
                and _cmp_versions(current_version, prerelease_version) < 0
            )
            if (
                latest_version
                and _cmp_versions(current_version, latest_version) > 0
                and not has_newer_pre
            ):
                print_progress(
                    "Smart Download Check",
                    "Current version is newer than latest stable. Aborting installation.",
                )
                return "abort_update"
        except Exception:
            pass

        # Check temp_update folder
        temp_package_path = TEMP_DIR
        temp_version = None
        if os.path.exists(temp_package_path):
            # Extract version from the downloaded package if exists
            for item in os.listdir(temp_package_path):
                if item.endswith(".zip"):
                    temp_version = os.path.splitext(item)[0]
                    break

        # Logic based on different cases
        if current_version == latest_version:
            # Case 1: Bot is up-to-date (stable). If a newer prerelease exists, let caller opt-in later.
            print_progress(
                "Smart Download Check",
                "Bot is already up-to-date. Aborting installation.",
            )
            if os.path.exists(temp_package_path):
                shutil.rmtree(temp_package_path, onerror=handle_remove_readonly)
                print_progress("Smart Download Check", "Temp folder deleted.")
            return (
                "abort_update_with_possible_prerelease"
                if prerelease_version
                and _cmp_versions(current_version, prerelease_version) < 0
                else "abort_update"
            )

        if temp_version and temp_version == latest_version:
            # Case 2: Temp package matches repository version
            print_progress(
                "Smart Download Check",
                "Skipping download, using existing temp package.",
            )
            return "skip_download"

        if (
            temp_version
            and latest_version
            and _cmp_versions(temp_version, latest_version) < 0
        ):
            # Case 3: Temp package is outdated
            print_progress(
                "Smart Download Check", "Temp package is outdated. Cleaning..."
            )
            shutil.rmtree(temp_package_path, onerror=handle_remove_readonly)
            print_progress("Smart Download Check", "Temp folder cleaned.")
            return "continue"

        if temp_version and _cmp_versions(temp_version, current_version) > 0:
            # Case 4: Temp package is invalid
            print_progress(
                "Smart Download Check", "Temp package is invalid. Cleaning..."
            )
            shutil.rmtree(temp_package_path, onerror=handle_remove_readonly)
            print_progress("Smart Download Check", "Temp folder cleaned.")
            return None

        # Default: Continue installation if none of the above
        print_progress(
            "Smart Download Check", "Condition not met, continuing installation."
        )
        return "continue"

    except Exception as e:
        log_error("Smart Download Check", "Failed during smart download check.", e)
        raise


def fetch_with_retries(url, headers, max_retries=5):
    """
    Fetches a URL with retries.
    """
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt + 1}/{max_retries}")
            time.sleep(2**attempt)
    raise Exception("Max retries exceeded.")


def fetch_update(repo_url, method, api_key=None, prefer_prerelease=False):
    """
    Handles fetching the update file with optional API key.
    """
    try:
        headers = {"Authorization": f"token {api_key}"} if api_key else {}
        releases_url = f"https://api.github.com/repos/{'/'.join(repo_url.rstrip('/').split('/')[-2:])}/releases"
        print(f"Fetching Release List From: {releases_url}")
        response = fetch_with_retries(releases_url, headers)
        releases = response.json()
        if not isinstance(releases, list) or not releases:
            raise ValueError("No releases found in repository.")
        # Select target release
        target = None
        if prefer_prerelease:
            target = next(
                (r for r in releases if not r.get("draft") and r.get("prerelease")),
                None,
            )
        if not target:
            target = next(
                (r for r in releases if not r.get("draft") and not r.get("prerelease")),
                None,
            )
        if not target:
            raise ValueError("No suitable release found (stable/prerelease).")
        release_data = target

        if method == "GET_FROM_RELEASE_SOURCE":
            # Get the source code URL for the latest tag
            download_url = release_data.get("zipball_url")
            if not download_url:
                raise ValueError("No source code URL found for the latest release.")
        elif method == "GET_FROM_RELEASE_PACKAGE":
            # Get the first asset URL if using release packages
            assets = release_data.get("assets", [])
            if not assets:
                raise ValueError("No assets found in the latest release.")
            download_url = assets[0]["browser_download_url"]
        else:
            raise ValueError(f"Unsupported update method: {method}")

        tag_name = release_data.get("tag_name", "latest")
        temp_zip_path = os.path.join(TEMP_DIR, f"{tag_name}.zip")

        print_progress("Download", "Downloading update...")
        os.makedirs(TEMP_DIR, exist_ok=True)
        with fetch_with_retries(download_url, headers) as download_response:
            with open(temp_zip_path, "wb") as temp_file:
                total_size = int(download_response.headers.get("content-length", 0))
                downloaded_size = 0
                for chunk in download_response.iter_content(chunk_size=1024):
                    if chunk:
                        temp_file.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            percent = (downloaded_size / total_size) * 100
                            print(f"Downloading: {percent:.2f}% Complete", end="\r")
                        else:
                            print("Downloading: Size unknown", end="\r")
        print_progress("Download", "Download complete.")
    except Exception as e:
        log_error("Download", "Failed to fetch update.", e)
        raise


def handle_remove_readonly(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def cleanup_root_directory(whitelist):
    """
    Cleans up all folders and files in the project root directory except those in the whitelist.
    """
    project_root = ROOT_DIR
    max_retries = 5
    retry_delay = 2  # seconds

    try:
        print_progress("Cleanup", "Starting cleanup process...")
        time.sleep(3)
        for item in os.listdir(project_root):
            item_path = os.path.join(project_root, item)
            if item not in whitelist or item == ".gitignore":
                if os.path.isdir(item_path):
                    for attempt in range(max_retries):
                        try:
                            shutil.rmtree(item_path, onerror=handle_remove_readonly)
                            print_progress(
                                f"Cleanup", f"Removed directory: {item_path}"
                            )
                            break
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print_progress(
                                    "Cleanup",
                                    f"Failed to remove directory: {item_path}. Reason: {e}. Retrying in {retry_delay} seconds...",
                                )
                                time.sleep(retry_delay)
                            else:
                                print_progress(
                                    "Cleanup",
                                    f"Failed to remove directory: {item_path}. Reason: {e}. Max retries reached.",
                                )
                                log_error(
                                    "Cleanup",
                                    f"Failed to remove directory {item_path}.",
                                    e,
                                )
                elif os.path.isfile(item_path):
                    for attempt in range(max_retries):
                        try:
                            os.remove(item_path)
                            print_progress(f"Cleanup", f"Removed file: {item_path}")
                            break
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print_progress(
                                    "Cleanup",
                                    f"Failed to remove file: {item_path}. Reason: {e}. Retrying in {retry_delay} seconds...",
                                )
                                time.sleep(retry_delay)
                            else:
                                print_progress(
                                    "Cleanup",
                                    f"Failed to remove file: {item_path}. Reason: {e}. Max retries reached.",
                                )
                                log_error(
                                    "Cleanup", f"Failed to remove file {item_path}.", e
                                )
        print_progress("Cleanup", "Cleanup complete.")
    except Exception as e:
        log_error("Cleanup", "Failed during cleanup.", e)
        raise


def extract_update(extract):
    if not extract:
        return
    try:
        print_progress("Extraction", "Extracting update files...")
        zip_files = [f for f in os.listdir(TEMP_DIR) if f.endswith(".zip")]
        if zip_files:
            first_zip_path = os.path.join(TEMP_DIR, zip_files[0])
            with zipfile.ZipFile(first_zip_path, "r") as zip_ref:
                zip_ref.extractall(TEMP_DIR)
        else:
            raise FileNotFoundError("No zip file found in the temp_update folder.")
        print_progress("Extraction", "Extraction complete.")
        log_error("Extraction", "Extraction complete.")
    except Exception as e:
        log_error("Extraction", "Failed to extract update.", e)
        raise


def move_extracted_contents_from_folder(move_contents_in_folder):
    print_progress(
        "Move Files", "Moving files from extracted folder to root directory..."
    )
    try:
        if move_contents_in_folder:
            extracted_folders = [
                f
                for f in os.listdir(TEMP_DIR)
                if os.path.isdir(os.path.join(TEMP_DIR, f))
            ]
            if not extracted_folders:
                raise FileNotFoundError(
                    "No extracted folder found in the temp_update folder."
                )
            extracted_folder = os.path.join(TEMP_DIR, extracted_folders[0])
            for item in os.listdir(extracted_folder):
                item_path = os.path.join(extracted_folder, item)
                shutil.move(item_path, os.path.join(ROOT_DIR, item))
            shutil.rmtree(extracted_folder)
        else:
            for item in os.listdir(TEMP_DIR):
                item_path = os.path.join(TEMP_DIR, item)
                if os.path.isdir(item_path):
                    for sub_item in os.listdir(item_path):
                        sub_item_path = os.path.join(item_path, sub_item)
                        shutil.move(sub_item_path, os.path.join(ROOT_DIR, sub_item))
                    shutil.rmtree(item_path)
                else:
                    shutil.move(item_path, os.path.join(ROOT_DIR, item))
    except Exception as e:
        log_error("Move Files", "Failed to move files from extracted folder.", e)
        raise
    print_progress("Move Files", "Files moved successfully.")
    log_error("Move Files", "Files moved successfully.")


def update_dependencies():
    print_progress("Dependencies", "Updating any outdated dependencies...")
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-U",
                "-r",
                os.path.join(ROOT_DIR, "requirements.txt"),
            ]
        )
        # Ensure wavelink is available (Lavalink client)
        try:
            import wavelink  # noqa: F401
        except Exception:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-U", "wavelink>=2.6.0"]
            )
        print_progress("Dependencies", "Dependencies updated successfully.")
    except subprocess.CalledProcessError as e:
        log_error("Dependencies", "Failed to update dependencies.", e)
        raise


def stop_bot_process():
    print_progress("Process", "Checking if bot is running...")
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe"],
            capture_output=True,
            text=True,
        )
        current_pid = os.getpid()
        if "python.exe" in result.stdout:
            print_progress("Process", "Stopping bot...")
            tasks = result.stdout.splitlines()
            for task in tasks:
                if "python.exe" in task:
                    pid = int(task.split()[1])
                    if pid != current_pid:
                        os.system(f"taskkill /f /pid {pid} /t")
            print_progress("Process", "Bot stopped successfully.")
        else:
            print_progress("Process", "Bot is not running. No need to stop.")
    except Exception as e:
        print_progress("Process", "Failed to check or stop the bot.")
        print(
            "<<<---Failed to check or stop the bot. Please stop the bot manually and run the script again.--->>>"
        )
        log_error("Process", "Failed to check or stop the bot.", e)
        raise
    print_progress("Process", "Bot stopped successfully.")


def check_files_required():
    print_progress("File Check", "Checking if required files exist...")
    try:
        if not os.path.exists(UPDATE_VARS_PATH):
            print_progress(
                "File Check", f"{UPDATE_VARS_PATH} not found. Aborting installation."
            )
            log_error(
                "File Check", f"{UPDATE_VARS_PATH} not found. Aborting installation."
            )
            print(
                "<<<---Required updateVars.json not found. Aborting installation.--->>>"
            )
            time.sleep(5)
            sys.exit(1)
        print_progress("File Check", "All required files exist.")
    except Exception as e:
        log_error("File Check", "Failed to check required files.", e)
        raise


def perform_ota_update():
    try:
        # Prequisite checks
        check_files_required()

        # Load update variables
        if not os.path.exists(UPDATE_VARS_PATH):
            raise FileNotFoundError(f"{UPDATE_VARS_PATH} not found.")

        with open(UPDATE_VARS_PATH, "r") as f:
            update_vars = json.load(f)

        whitelist = set(update_vars.get(WHITELISTED_FILES_KEY, []))
        repo_url = update_vars["REPOSITORY_URL"]
        update_method = update_vars["UPDATE_GET_METHOD"]
        extract_files = update_vars.get("EXTRACT_FILES", False)
        move_contents_in_folder = update_vars.get("MOVE_CONTENTS_IN_FOLDER", False)
        smart_download_enabled = update_vars.get("SMART_DOWNLOAD_ENABLED", False)
        api_key = update_vars.get("REPO_API_KEY", "")

        # New: parse CLI preference override
        prefer_override = None
        if "--prefer-prerelease" in sys.argv:
            prefer_override = True
        elif "--prefer-stable" in sys.argv:
            prefer_override = False

        # Prequisite checks 2
        if smart_download_enabled:
            result = smart_download_check()
        else:
            print_progress(
                "Smart Download Check",
                "Smart download disabled. It will now continue with the update.",
            )
            result = "continue"

        # Check for prerelease availability and optionally opt-in
        prefer_prerelease = False
        try:
            cu = check_update()
            current_version = cu.get("current_version")
            pre_ver = cu.get("prerelease_version")
            pre_avail = (
                cu.get("prerelease_available", False)
                and pre_ver
                and pre_ver != current_version
            )
            stable_ver = cu.get("stable_version")

            if pre_avail:
                if prefer_override is not None:
                    prefer_prerelease = bool(prefer_override)
                    if (
                        result
                        in ("abort_update", "abort_update_with_possible_prerelease")
                        and prefer_prerelease
                    ):
                        result = "continue"
                    print_progress(
                        "Preference",
                        f"Using CLI preference: {'prerelease' if prefer_prerelease else 'stable'}",
                    )
                else:
                    print()
                    print("A prerelease build is available:")
                    print(f"- Stable latest: {stable_ver or 'N/A'}")
                    print(f"- Prerelease: {pre_ver} (may be buggy or unstable)")
                    choice = (
                        input("Do you want to install the prerelease? [y/N]: ")
                        .strip()
                        .lower()
                    )
                    prefer_prerelease = choice == "y"
                    if (
                        result
                        in ("abort_update", "abort_update_with_possible_prerelease")
                        and prefer_prerelease
                    ):
                        result = "continue"
            else:
                # If no prerelease or not newer, still respect explicit stable override silently
                if prefer_override is False:
                    print_progress("Preference", "Using CLI preference: stable")
        except Exception:
            pass

        if result in ("abort_update",):
            print("<<<---Bot is already up-to-date. Aborting installation.--->>>")
            time.sleep(10)
            sys.exit(0)

        # Stop the bot
        stop_bot_process()

        # Update Dependencies
        update_dependencies()

        # Fetch the update file
        if result == "continue" or not result:
            try:
                chosen_tag = None
                if prefer_prerelease:
                    chosen_tag = cu.get("prerelease_version")
                else:
                    chosen_tag = cu.get("stable_version") or cu.get("latest_version")
                if chosen_tag:
                    existing_zip = os.path.join(TEMP_DIR, f"{chosen_tag}.zip")
                    if os.path.exists(existing_zip):
                        print_progress(
                            "Download", "Found existing package. Skipping download."
                        )
                    else:
                        fetch_update(
                            repo_url,
                            update_method,
                            api_key,
                            prefer_prerelease=prefer_prerelease,
                        )
                else:
                    fetch_update(
                        repo_url,
                        update_method,
                        api_key,
                        prefer_prerelease=prefer_prerelease,
                    )
            except Exception:
                fetch_update(
                    repo_url,
                    update_method,
                    api_key,
                    prefer_prerelease=prefer_prerelease,
                )

        # Cleanup
        time.sleep(2)
        cleanup_root_directory(whitelist)

        # Extract and move files
        time.sleep(2)
        if result == "continue" or not result:
            extract_update(extract_files)
        move_extracted_contents_from_folder(move_contents_in_folder)

        # Cleanup residuals
        time.sleep(2)
        print_progress("Post-Cleanup", "Removing temporary files...")
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        print_progress("Post-Cleanup", "Temporary files removed.")

        # Start the bot
        time.sleep(1)
        print_progress("Process", "Starting bot...")
        subprocess.Popen(["python", os.path.join(ROOT_DIR, "main.py")], close_fds=True)
        print_progress("Process", "Bot started successfully.")
        print("<<<---OTA Update completed successfully. The bot will now start.--->>>")
        # Exit updater
        time.sleep(5)
        sys.exit(0)
    except Exception as e:
        log_error(
            "OTA Update",
            "Update failed and is Aborted, Please check the stack trace above for more Information",
            e,
        )
        print(
            "<<<---Update failed and is Aborted, Please check the logs for more Information. This window will now close in 10 seconds.--->>>"
        )
        time.sleep(10)
        sys.exit(1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        perform_ota_update()
    else:
        try:
            logo_message(f"OTA Automatic Update Script")
            print_progress("Main", "Starting OTA Update Worker...")
            time.sleep(3)
            subprocess.Popen(
                ["start", "cmd", "/c", sys.executable, __file__, "worker"],
                cwd=ROOT_DIR,
                shell=True,
            )
        except Exception as e:
            log_error("Main", "Failed to start worker process.", e)
        sys.exit(0)


if __name__ == "__main__":
    main()
