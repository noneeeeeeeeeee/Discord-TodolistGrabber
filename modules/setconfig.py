import os
import json

def create_default_config(guild_id, default_admin_role_id, default_role_id, default_ping_role_id, music_dj_role):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    os.makedirs(config_dir, exist_ok=True)
    
    config_data = {
        "DefaultAdmin": default_admin_role_id,
        " ": None,
        "_comment2": "Noticeboard Config",
        " ": None,
        "DefaultRoleId": default_role_id,
        "NoticeBoardChannelId": "Default",
        "NoticeBoardUpdateInterval": None,
        "PingRoleId": default_ping_role_id,
        "PingDailyTime": "15:00",
        "noticeboardEditID": [],
        "pingmessageEditID": None,
        "pingDateTime": None,
        " ": None,
        "_comment1": "Music Config",
        " ": None,
        "MusicEnabled": False,
        "MusicDJRole": music_dj_role,
        "MusicDJRoleRequired": True,
        "MusicVolume": 0.5,
        "MusicQueueLimit": 10,
        "MusicQueueLimitEnabled": True,
        "MusicPlayerStick": True,
        "TrackMaxDuration": 600,
        "RemoveNonSongsUsingSponsorBlock": True,
        "PlaylistAddLimit": 10,
        "_comment1": "Google Classroom Config",
        "GoogleClassroomEnabled": False,
        "DefaultChannelId": "Default",
        " ": None,
    }
    
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    with open(config_file_path, 'w') as config_file:
        json.dump(config_data, config_file, indent=4)


def edit_noticeboard_config(guild_id, noticeboard_channelid=None, noticeboard_updateinterval=None, PingDailyTime = None):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    
    if not os.path.exists(config_file_path):
        raise FileNotFoundError(f"Config file for guild_id {guild_id} does not exist.")
    
    with open(config_file_path, 'r') as config_file:
        config_data = json.load(config_file)
    
    if noticeboard_channelid is not None:
        config_data["NoticeBoardChannelId"] = noticeboard_channelid
    
    if noticeboard_updateinterval is not None:
        config_data["NoticeBoardUpdateInterval"] = noticeboard_updateinterval
    
    if PingDailyTime is not None:
        config_data["PingDailyTime"] = PingDailyTime
    
    with open(config_file_path, 'w') as config_file:
        json.dump(config_data, config_file, indent=4)


def edit_json_file(guild_id, key, value):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    
    if not os.path.exists(config_file_path):
        raise FileNotFoundError(f"Config file for guild_id {guild_id} does not exist.")
    
    with open(config_file_path, 'r') as config_file:
        config_data = json.load(config_file)
    try:
        config_data[key] = value
    except KeyError:
        raise KeyError(f"Key {key} does not exist in the config file.")
    
    with open(config_file_path, 'w') as config_file:
        json.dump(config_data, config_file, indent=4)

def check_guild_config_available(guild_id):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    return os.path.exists(config_file_path)

def check_admin_role(guild_id, user_roles):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    
    if not os.path.exists(config_file_path):
        return False
    
    with open(config_file_path, 'r') as config_file:
        config_data = json.load(config_file)
        default_admin_role_id = config_data["DefaultAdmin"]
        
        # Check if any of the user's roles match the default admin role
        return any(role_id == default_admin_role_id for role_id in user_roles)


def json_get(guild_id):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    with open(config_file_path, 'r') as config_file:
        return json.load(config_file)


    

