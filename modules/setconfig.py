import os
import json

def create_default_config(guild_id, default_admin_role_id, default_role_id, default_ping_role_id):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    os.makedirs(config_dir, exist_ok=True)
    
    config_data = {
        "DefaultAdmin": default_admin_role_id,
        "NoticeBoardChannelId": "Default",
        "NoticeBoardUpdateInterval": None,

        "DefaultRoleId": default_role_id,
        "PingRoleId": default_ping_role_id,

        "MusicEnabled": False,
        "MusicDJRole": "Default",
        "MusicDJRoleRequired": False,
        "MusicVolume": 0.5,
        "MusicQueueLimit": 10,
        "MusicQueueLimitEnabled": False,
    }
    
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    with open(config_file_path, 'w') as config_file:
        json.dump(config_data, config_file, indent=4)


def edit_noticeboard_config(noticeboard_channelid, noticeboard_updateinterval, guild_id):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    os.makedirs(config_dir, exist_ok=True)
    
    config_data = {
        "NoticeBoardChannelId": noticeboard_channelid,
        "NoticeBoardUpdateInterval": noticeboard_updateinterval,
    }
    
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
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
    

