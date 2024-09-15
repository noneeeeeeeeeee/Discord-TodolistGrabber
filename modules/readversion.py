import os
import requests

def read_current_version():
    with open(os.path.join(os.path.dirname(__file__), '..', 'version.txt')) as version_file:
        return version_file.read().strip()
        
def read_latest_version():
    try:
        response = requests.get('https://github.com/noneeeeeeeeeee/Discord-TodolistGrabber/latest', timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except requests.RequestException as e:
        return f"Error fetching latest version: {e}"
# Broken, Use only read_current_version()