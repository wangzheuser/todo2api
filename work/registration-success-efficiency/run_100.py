#!/usr/bin/env python3
"""Run the requested 100-account live verification independently of the console."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REG_DIR = ROOT / "reg"
STATUS_FILE = Path(__file__).with_name("run_100_status.json")
sys.path.insert(0, str(REG_DIR))

import start_reg


settings = start_reg.load_settings()
settings.update(
    {
        "count": 100,
        "threads": 5,
        "max_retries": 5,
        "turnstile_concurrency": 5,
        "turnstile_mode": "always",
        "otp_interval_seconds": 40,
        "otp_cooldown_seconds": 3600,
        "network_cooldown_seconds": 60,
        "proxy_platforms": "node,jp,us,de,github,baokemeng",
        "mailpoolhub_provider": "random",
    }
)
environment = os.environ.copy()
environment["MAILPOOLHUB_API_KEY"] = settings["mailpoolhub_api_key"]
key_file = REG_DIR / "get_apikey.txt"
before = len(key_file.read_text(encoding="utf-8").splitlines()) if key_file.exists() else 0
started = datetime.now().isoformat(timespec="seconds")
exit_code = start_reg.run_registration(start_reg.build_command(settings), environment)
after = len(key_file.read_text(encoding="utf-8").splitlines()) if key_file.exists() else 0
STATUS_FILE.write_text(
    json.dumps(
        {
            "started_at": started,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "exit_code": exit_code,
            "key_lines_before": before,
            "key_lines_after": after,
            "key_lines_added": after - before,
        },
        indent=2,
    ),
    encoding="utf-8",
)
raise SystemExit(exit_code)
