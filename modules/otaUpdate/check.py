import os
import json
import requests

# Define the project root directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_VARS_PATH = os.path.join(SCRIPT_DIR, "updateVars.json")
# Load update variables
if not os.path.exists(UPDATE_VARS_PATH):
    raise FileNotFoundError(f"{UPDATE_VARS_PATH} not found.")

with open(UPDATE_VARS_PATH, "r") as f:
    update_vars = json.load(f)
root_dir_depth = update_vars.get("PROJECT_ROOT_FROM_OTA_SCRIPT_LOCATION", 0)
ROOT_DIR = SCRIPT_DIR
for _ in range(root_dir_depth):
    ROOT_DIR = os.path.dirname(ROOT_DIR)

# File paths
VERSION_FILE_PATH = os.path.join(ROOT_DIR, "version.txt")


def check_update():
    # Check for required files
    if not os.path.exists(UPDATE_VARS_PATH):
        raise FileNotFoundError(f"{UPDATE_VARS_PATH} not found.")
    if not os.path.exists(VERSION_FILE_PATH):
        raise FileNotFoundError(f"{VERSION_FILE_PATH} not found.")

    # Load repository URL and API key from updateVars.json
    with open(UPDATE_VARS_PATH, "r") as f:
        update_vars = json.load(f)

    repo_url = update_vars.get("REPOSITORY_URL")
    api_key = update_vars.get("REPO_API_KEY", "").strip()

    if not repo_url:
        raise ValueError("REPOSITORY_URL is missing in updateVars.json.")

    # GitHub API endpoint for the latest release
    latest_release_api = f"https://api.github.com/repos/{'/'.join(repo_url.rstrip('/').split('/')[-2:])}/releases/latest"

    # Load the current version from version.txt
    with open(VERSION_FILE_PATH, "r") as f:
        current_version = f.read().strip().split("-")[0]

    try:
        # Set headers for API request if an API key is provided
        headers = {"Authorization": f"token {api_key}"} if api_key else {}

        # Fetch the latest release version tag
        response = requests.get(latest_release_api, headers=headers)
        response.raise_for_status()
        release_data = response.json()
        latest_version = release_data.get("tag_name")
        changelog = release_data.get("body")

        if not latest_version:
            return {
                "status": "error",
                "message": "Could not fetch the latest version tag from the repository.",
            }

        # Compare versions
        if current_version == latest_version:
            return {
                "status": "up-to-date",
                "current_version": current_version,
                "latest_version": latest_version,
                "changelog": changelog,
            }
        else:
            return {
                "status": "update-available",
                "current_version": current_version,
                "latest_version": latest_version,
                "changelog": changelog,
            }
    except requests.exceptions.RequestException as e:
        return {"status": "error", "message": f"HTTP request failed: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to check for updates: {e}"}


if __name__ == "__main__":
    result = check_update()
    print(result)
