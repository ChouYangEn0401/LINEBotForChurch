# Notification-area icons for the back end and the LINE commands tunnel (see src/church_bot/tray.py).
# Run with pythonw (no console window). No argument = open both icons; "web" / "webhook" = just that one.
# Opened by the Startup-folder shortcut that service\install.bat puts there, and by 2-start / 3-open-webhook.
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)

from church_bot.tray import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
