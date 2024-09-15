import os
import json

def create_default_config(guild_id, default_admin_role_id, default_role_id):
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    os.makedirs(config_dir, exist_ok=True)
    
    config_data = {
        "Default Admin": default_admin_role_id,
        "NoticeBoardChannelId": "Default",
        "NoticeBoardUpdateInterval": None,

        "DefaultRoleId": default_role_id,

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



