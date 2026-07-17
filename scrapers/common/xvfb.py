import os
import subprocess
import time
import platform

def ensure_xvfb(display=":99"):
    # If a display is already set, do nothing

    if platform.system() != "Linux":
        return

    if os.getenv("DISPLAY"):
        return

    # Start Xvfb in the background
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Give Xvfb time to initialize
    time.sleep(0.4)

    # Export DISPLAY for this process
    os.environ["DISPLAY"] = display
