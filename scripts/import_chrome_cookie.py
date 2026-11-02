#!/usr/bin/env python3
"""macOS: import ONLY MoodleSession from an open edu.hse.ru Chrome tab."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    os.umask(0o077)
    javascript = (
        "document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('MoodleSession=')) || ''"
    )
    script = f"""tell application "Google Chrome"
repeat with w in windows
repeat with t in tabs of w
if URL of t starts with "https://edu.hse.ru/" then
set result to execute t javascript {json.dumps(javascript)}
if result is not "" then return result
end if
end repeat
end repeat
return ""
end tell"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(
            "В Chrome включите Вид → Разработчикам → Разрешить JavaScript "
            "из событий Apple; откройте авторизованную вкладку edu.hse.ru."
        )
    cookie = result.stdout.strip()
    if not re.fullmatch(r"MoodleSession=[A-Za-z0-9._~-]+", cookie):
        raise SystemExit("MoodleSession не найдена. Войдите в edu.hse.ru в Chrome.")
    path = Path(args.env_file)
    old = path.read_text() if path.exists() else ""
    old = re.sub(r"^LMS_COOKIE_HEADER=.*(?:\n|$)", "", old, flags=re.MULTILINE)
    path.write_text(old.rstrip() + f"\nLMS_COOKIE_HEADER='{cookie}'\n")
    path.chmod(0o600)
    print("MoodleSession сохранена в .env (0600). Значение не выводится.")


if __name__ == "__main__":
    main()
