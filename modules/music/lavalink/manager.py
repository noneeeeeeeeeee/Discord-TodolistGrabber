import os
import socket
import subprocess
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LL_DIR = BASE_DIR  # modules/music/lavalink
JAR_NAME = "Lavalink.jar"
APP_YML = "application.yml"
JAVA = os.getenv("LAVALINK_JAVA_PATH", "java")


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _wait_for_port(host: str, port: int, timeout: float = 45.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if _is_port_open(host, port):
            return True
        time.sleep(1)
    return False


def _print_manual_setup_instructions(host: str, port: int, password: str):
    jar_dir = LL_DIR
    app_yml_path = os.path.join(jar_dir, APP_YML)
    print("\n========== Lavalink Setup Required ==========")
    print("A Lavalink node is not running or misconfigured.")
    print("Please download Lavalink.jar and place it here:")
    print(f" - {jar_dir}")
    print(
        f"Create an application.yml next to the jar (path: {app_yml_path}) with at least:"
    )
    print("------------------------------------------------------------")
    print(
        f"""server:
  port: {port}
  address: 0.0.0.0

lavalink:
  server:
    password: "{password}"
    sources:
      youtube: true
      bandcamp: true
      soundcloud: true
      twitch: true
      vimeo: true
      http: true
      local: false
"""
    )
    print("------------------------------------------------------------")
    print("Optionally you can run it manually:")
    print("  java -jar Lavalink.jar")
    print("The bot will auto-start it on launch when both files exist.\n")


async def ensure_local_node(host: str, port: int, password: str, secure: bool) -> bool:
    """
    Start a local Lavalink if Lavalink.jar and application.yml are present.
    Returns True if a node is running (already or started), else prints instructions and returns False.
    """
    # Already running?
    if _is_port_open(host, port):
        return True

    jar_path = os.path.join(LL_DIR, JAR_NAME)
    yml_path = os.path.join(LL_DIR, APP_YML)

    if not os.path.exists(jar_path) or not os.path.exists(yml_path):
        _print_manual_setup_instructions(host, port, password)
        return False

    # Launch Lavalink detached
    start_kwargs = {}
    if os.name == "nt":
        start_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
        start_kwargs["close_fds"] = True

    try:
        subprocess.Popen(
            [JAVA, "-jar", JAR_NAME],
            cwd=LL_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **start_kwargs,
        )
    except Exception as e:
        print(f"Failed to launch local Lavalink: {e}")
        _print_manual_setup_instructions(host, port, password)
        return False

    # Wait for it to be ready
    ok = _wait_for_port(host, port, timeout=45.0)
    if not ok:
        print(
            "Lavalink did not open the port in time. Please check application.yml and Java installation."
        )
    return ok
    subprocess.Popen(
        [JAVA, "-jar", JAR_NAME],
        cwd=LL_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **start_kwargs,
    )

    # Wait for port to open
    ok = await _wait_for_port(host, port, timeout=45.0)
    return ok
