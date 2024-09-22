import datetime
from datetime import timedelta
from .apicall import fetch_api_data
import os
import json
import re

def cache_data(weekSelect=None):
    data = fetch_api_data(weekSelect, True)
    cache_dir = os.path.join(os.path.dirname(__file__), '..', 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    data_dict = json.loads(data)
    apicalltime = data_dict["Status"][0]["apicalltime"]

    if 'GMT+7' in apicalltime:
        apicalltime = apicalltime.replace('GMT+7', '+0700')

    apicalltime_dt = datetime.datetime.strptime(apicalltime, "%a, %d %b %Y %H:%M:%S %z")
    formatted_time = apicalltime_dt.strftime("%M-%H_%d_%m_%Y")
    if weekSelect is not None:
        formatted_time += f'_week_{weekSelect}'

    cache_file = os.path.join(cache_dir, f'cache_{formatted_time}.json')
    with open(cache_file, 'w') as f:
        f.write(data)

def cachecleanup():
    try:
        cache_dir = os.path.join(os.path.dirname(__file__), '..', 'cache')
        if not os.path.isdir(cache_dir):
            raise NotADirectoryError(f"{cache_dir} is not a valid directory.")
    except Exception as e:
        print(f"Error: {e}")
        return
    weeks = datetime.datetime.now() - timedelta(weeks=1)
    
    removed_files_count = 0
    for file in os.listdir(cache_dir):
        if file.endswith(".json"):
            file_path = os.path.join(cache_dir, file)
            # Extract the date part from the filename
            date_part = file.split('_')[1:5]
            datetime_str = '_'.join(date_part)
            date_str = '_'.join(datetime_str.split('_')[1:4])
            if date_str.endswith('.json'):
                date_str = date_str[:-5]
            
            file_date = datetime.datetime.strptime(date_str, "%d_%m_%Y")
            if file_date < weeks:
                os.remove(file_path)
                print(f"Removed {file_path}")
                removed_files_count += 1

    if removed_files_count == 0:
        return "No files to remove"
    else:
        return f"Removed {removed_files_count} file(s)"




def delete_redundant_cache_files():
    cache_dir = os.path.join(os.path.dirname(__file__), '..', 'cache')
    cache_file_pattern = re.compile(r'cache_(\d{2})-(\d{2})_(\d{2})_(\d{2})_(\d{4})_week_all')
    latest_files = {}

    for filename in os.listdir(cache_dir):
        match = cache_file_pattern.match(filename)
        if match:
            minute = match.group(1)
            hour = match.group(2)
            day = match.group(3)
            month = match.group(4)
            year = match.group(5)
            time_obj = datetime.datetime.strptime(f'{hour}:{minute}', '%H:%M')
            date_obj = datetime.datetime.strptime(f'{day}_{month}_{year}', '%d_%m_%Y')
            if date_obj not in latest_files:
                latest_files[date_obj] = (time_obj, filename)
            else:
                if time_obj > latest_files[date_obj][0]:
                    latest_files[date_obj] = (time_obj, filename)

    for filename in os.listdir(cache_dir):
        match = cache_file_pattern.match(filename)
        if match:
            minute = match.group(1)
            hour = match.group(2)
            day = match.group(3)
            month = match.group(4)
            year = match.group(5)
            time_obj = datetime.datetime.strptime(f'{hour}:{minute}', '%H:%M')
            date_obj = datetime.datetime.strptime(f'{day}_{month}_{year}', '%d_%m_%Y')
            if filename != latest_files[date_obj][1]:
                file_path = os.path.join(cache_dir, filename)
                os.remove(file_path)
                print(f"Deleted old cache file: {filename}")





def truncate_cache():
    try:
        cache_dir = os.path.join(os.path.dirname(__file__), '..', 'cache')
        if not os.path.isdir(cache_dir):
            raise NotADirectoryError(f"{cache_dir} is not a valid directory.")
    except Exception as e:
        print(f"Error: {e}")
        return
    
    for filename in os.listdir(cache_dir):
        file_path = os.path.join(cache_dir, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                os.rmdir(file_path)
        except Exception as e:
            return f'Failed to delete {file_path}. Reason: {e}'
    return "Cache truncated"

def cache_read(weekSelect=None):
    cache_dir = os.path.join(os.path.dirname(__file__), '..', 'cache')
    if not os.path.isdir(cache_dir):
        raise NotADirectoryError(f"{cache_dir} is not a valid directory.")
    
    latest_file = None
    latest_time = None
    
    for file in os.listdir(cache_dir):
        date_part = None
        if weekSelect is None:
            if file.startswith("cache_") and file.endswith(".json") and "_week_" not in file:
                date_part = file.split('_')[1:5]
        else:
            if file.startswith(f"cache_") and file.endswith(f"_week_{weekSelect}.json"):
                date_part = file.split('_')[1:5]
        
        if date_part:
            datetime_str = '_'.join(date_part)
            date_str = '_'.join(datetime_str.split('_')[1:4])
            if date_str.endswith('.json'):
                date_str = date_str[:-5]
            
            file_date = datetime.datetime.strptime(date_str, "%d_%m_%Y")
            if latest_time is None or file_date > latest_time:
                latest_time = file_date
                latest_file = file
    
    if latest_file:
        with open(os.path.join(cache_dir, latest_file), 'r') as f:
            return f.read()
    else:
        return None
    
