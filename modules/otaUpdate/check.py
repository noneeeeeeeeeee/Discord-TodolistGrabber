import os
import json
import requests

# File paths
UPDATE_VARS_PATH = "./updateVars.json"
VERSION_FILE_PATH = "./version.txt"


def check_update():
    if not os.path.exists(UPDATE_VARS_PATH):
        raise FileNotFoundError(f"{UPDATE_VARS_PATH} not found.")

    if not os.path.exists(VERSION_FILE_PATH):
        raise FileNotFoundError(f"{VERSION_FILE_PATH} not found.")

    # Load repository URL from updateVars.json
    with open(UPDATE_VARS_PATH, "r") as f:
        update_vars = json.load(f)

    repo_url = update_vars.get("REPOSITORY_URL")
    if not repo_url:
        raise ValueError("REPOSITORY_URL is missing in updateVars.json.")

    # GitHub API endpoint for the latest release
    latest_release_api = f"https://api.github.com/repos/{'/'.join(repo_url.rstrip('/').split('/')[-2:])}/releases/latest"

    # Load the current version from version.txt
    with open(VERSION_FILE_PATH, "r") as f:
        current_version = f.read().strip()

    try:
        # Fetch the latest release version tag
        response = requests.get(latest_release_api)
        response.raise_for_status()
        latest_version = response.json().get("tag_name")

        if not latest_version:
            return {
                "status": "error",
                "message": "Could not fetch the latest version tag from the repository.",
            }

        # Compare versions
        if current_version == latest_version:
            return {"status": "up-to-date", "current_version": current_version}
        else:
            return {
                "status": "update-available",
                "current_version": current_version,
                "latest_version": latest_version,
            }
    except Exception as e:
        return {"status": "error", "message": f"Failed to check for updates: {e}"}


if __name__ == "__main__":
    result = check_update()
    print(result)
