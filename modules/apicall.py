import requests
import json
import os
from enviromentfilegenerator import check_and_load_env_file

def fetch_api_data(week = None, status = False):
    check_and_load_env_file()
    base_url = os.getenv('API_URL')

    if week is not None:
        api = f"{base_url}?output=json&weeks={week}"
    else:
        api = f"{base_url}?output=json"

    try:
        response = requests.get(api, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return f"Error: {e}"
    if status:
        responsetime = str(response.elapsed.total_seconds())
        apicalltime = str(response.headers['Date'])
        response_json = response.json()
        response_json["Status"] = [
            {
                "responsetime": responsetime,
                "apicalltime": apicalltime
            }
        ]
        return json.dumps(response_json)
    else:
        return response.text
        

print(fetch_api_data())

