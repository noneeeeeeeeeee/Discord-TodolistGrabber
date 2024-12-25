import os
import json
import shutil
import subprocess
import requests
import zipfile
import datetime
import sys
import time

# File paths and constants
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_VARS_PATH = os.path.abspath(
    os.path.join(SCRIPT_DIR, "..", "..", "updateVars.json")
)
LOGS_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "logs"))
TEMP_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "temp_update"))

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
    log_path = os.path.join(LOGS_DIR, f"crash_{now}.txt")
    with open(log_path, "a") as log_file:
        log_file.write(f"[{stage}] {message}\n")
        if exception:
            log_file.write(str(exception) + "\n")


def print_progress(stage, message):
    print(f"[{stage}] {message}")
    log_error(stage, message)


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
            print(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            time.sleep(2**attempt)
    raise Exception("Max retries exceeded.")


def fetch_update(repo_url, method, api_key=None):
    """
    Handles fetching the update file with optional API key.
    """
    try:
        headers = {"Authorization": f"token {api_key}"} if api_key else {}
        release_url = f"https://api.github.com/repos/{'/'.join(repo_url.rstrip('/').split('/')[-2:])}/releases/latest"
        print(f"Release URL: {release_url}")
        print(f"Headers: {headers}")

        response = fetch_with_retries(release_url, headers)
        print(f"Response Status Code: {response.status_code}")
        print(f"Response JSON: {response.json()}")

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

        print(f"Download URL: {download_url}")

        print_progress("Download", "Downloading update...")
        os.makedirs(TEMP_DIR, exist_ok=True)
        with fetch_with_retries(download_url, headers) as download_response:
            with open(TEMP_ZIP_PATH, "wb") as temp_file:
                total_size = int(download_response.headers.get("content-length", 0))
                downloaded_size = 0
                for chunk in download_response.iter_content(chunk_size=1024):
                    if chunk:
                        temp_file.write(chunk)
                        downloaded_size += len(chunk)
                        percent = (downloaded_size / total_size) * 100
                        print(f"Downloading... {percent:.2f}% Complete", end="\r")
        print_progress("Download", "Download complete.")
    except Exception as e:
        log_error("Download", "Failed to fetch update.", e)
        raise


def cleanup_root_directory(whitelist):
    """
    Cleans up all files and folders in the root directory except those in the whitelist.
    """
    try:
        print_progress("Cleanup", "Cleaning up non-whitelisted files and folders...")
        for item in os.listdir("."):
            if item not in whitelist:
                if os.path.isfile(item) or os.path.islink(item):
                    os.unlink(item)
                elif os.path.isdir(item):
                    shutil.rmtree(item)
        print_progress("Cleanup", "Cleanup complete.")
    except Exception as e:
        log_error("Cleanup", "Failed during cleanup.", e)
        raise


def extract_update(extract):
    """
    Extracts the downloaded update if `extract` is true.
    """
    if extract:
        try:
            print_progress("Extraction", "Extracting update files...")
            with zipfile.ZipFile(TEMP_ZIP_PATH, "r") as zip_ref:
                zip_ref.extractall(".")
            print_progress("Extraction", "Extraction complete.")
        except Exception as e:
            log_error("Extraction", "Failed to extract update.", e)
            raise


def perform_ota_update():
    """
    Performs the OTA update process and tracks progress in console and logs.
    """
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
        api_key = update_vars.get("REPO_API_KEY", "")

        # Fetch the update file
        fetch_update(repo_url, update_method, api_key)

        # Cleanup root directory
        cleanup_root_directory(whitelist)

        # Extract update
        extract_update(extract_files)

        # Cleanup residuals
        print_progress("Post-Cleanup", "Removing temporary files...")
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        print_progress("Post-Cleanup", "Temporary files removed.")

        # Start the bot
        print_progress("Restart", "Starting bot...")
        subprocess.Popen(["python", "main.py"], close_fds=True)
        print_progress("Restart", "Bot started successfully.")

        # Exit updater
        time.sleep(5)
        sys.exit(0)
    except Exception as e:
        log_error("OTA Update", "Update failed.", e)
        sys.exit(1)


def main():
    """
    Main function that runs `startOTA.py` in a subprocess with a new command prompt window for self-update capability.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        # Worker mode: Perform OTA update
        perform_ota_update()
    else:
        # Parent process: Spawn a new process for the updater in a new command prompt window
        subprocess.Popen(
            ["start", "cmd", "/k", sys.executable, __file__, "worker"],
            shell=True,
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
