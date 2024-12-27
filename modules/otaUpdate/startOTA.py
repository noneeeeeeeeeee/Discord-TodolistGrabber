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
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
UPDATE_VARS_PATH = os.path.join(ROOT_DIR, "updateVars.json")
LOGS_DIR = os.path.join(ROOT_DIR, "ota_logs")
TEMP_DIR = os.path.join(ROOT_DIR, "temp_update")

# Ensure the logs and temp directories exist
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

TEMP_ZIP_PATH = os.path.join(TEMP_DIR, "update.zip")
WHITELISTED_FILES_KEY = "WHITELISTED_FILES_FOLDERS"


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
        latest_version = result.get("latest_version")

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
            # Case 1: Bot is up-to-date
            print_progress(
                "Smart Download Check",
                "Bot is already up-to-date. Aborting installation.",
            )
            if os.path.exists(temp_package_path):
                shutil.rmtree(temp_package_path, onerror=handle_remove_readonly)
                print_progress("Smart Download Check", "Temp folder deleted.")
            return "abort_update"

        if temp_version and temp_version == latest_version:
            # Case 2: Temp package matches repository version
            print_progress(
                "Smart Download Check",
                "Skipping download, using existing temp package.",
            )
            return "skip_download"

        if temp_version and temp_version < latest_version:
            # Case 3: Temp package is outdated
            print_progress(
                "Smart Download Check", "Temp package is outdated. Cleaning..."
            )
            shutil.rmtree(temp_package_path, onerror=handle_remove_readonly)
            print_progress("Smart Download Check", "Temp folder cleaned.")
            return "continue"

        if temp_version and temp_version > current_version:
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


def fetch_update(repo_url, method, api_key=None):
    """
    Handles fetching the update file with optional API key.
    """
    try:
        headers = {"Authorization": f"token {api_key}"} if api_key else {}
        release_url = f"https://api.github.com/repos/{'/'.join(repo_url.rstrip('/').split('/')[-2:])}/releases/latest"
        print(f"Fetching Update File From: {release_url}")

        response = fetch_with_retries(release_url, headers)
        release_data = response.json()

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
        with zipfile.ZipFile(TEMP_ZIP_PATH, "r") as zip_ref:
            zip_ref.extractall(TEMP_DIR)
        print_progress("Extraction", "Extraction complete.")
        log_error("Extraction", "Extraction complete.")
    except Exception as e:
        log_error("Extraction", "Failed to extract update.", e)
        raise


def move_extracted_contents_from_folder(move_contents_in_folder):
    print_progress(
        "Move Files", "Moving files from extracted folder to root directory..."
    )
    access_extracted_folder = os.listdir(TEMP_DIR)
    try:
        if move_contents_in_folder:
            extracted_folder = os.path.join(TEMP_DIR, access_extracted_folder[0])
            for item in os.listdir(extracted_folder):
                item_path = os.path.join(extracted_folder, item)
                shutil.move(item_path, os.path.join(ROOT_DIR, item))
            shutil.rmtree(extracted_folder)
        else:
            for item in access_extracted_folder:
                item_path = os.path.join(TEMP_DIR, item)
                shutil.move(item_path, os.path.join(ROOT_DIR, item))
    except Exception as e:
        log_error("Move Files", "Failed to move files from extracted folder.", e)
        raise
    print_progress("Move Files", "Files moved successfully.")
    log_error("Move Files", "Files moved successfully.")


def perform_ota_update():
    try:
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

        # Prequisite checks
        if smart_download_enabled:
            result = smart_download_check()
        else:
            print_progress(
                "Smart Download Check",
                "Smart download disabled. It will now continue with the update.",
            )
            result = "continue"

        # Fetch the update file
        if result == "continue" or not result:
            fetch_update(repo_url, update_method, api_key)

        if result == "abort_update":
            print("<<<---Bot is already up-to-date. Aborting installation.--->>>")
            time.sleep(10)
            sys.exit(0)
        # Cleanup
        cleanup_root_directory(whitelist)

        # Extract and move files
        if result == "continue" or not result:
            extract_update(extract_files)
        move_extracted_contents_from_folder(move_contents_in_folder)

        # Cleanup residuals
        print_progress("Post-Cleanup", "Removing temporary files...")
        # shutil.rmtree(TEMP_DIR, ignore_errors=True)
        print_progress("Post-Cleanup", "Temporary files removed.")

        # Start the bot
        print_progress("Restart", "Starting bot...")
        # subprocess.Popen(["python", os.path.join(SCRIPT_DIR, "..", "..", "main.py")], close_fds=True)
        print_progress("Restart", "Bot started successfully.")

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
