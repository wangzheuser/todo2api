#!/usr/bin/env python3
"""Generate the selectable provider catalog and benchmark report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REG_DIR = ROOT / "reg"
RESULTS_FILE = Path(__file__).with_name("results.json")
REPORT_FILE = Path(__file__).with_name("REPORT.md")
WORKING_FILE = REG_DIR / "working_mail_channels.json"
REJECTED_FILE = REG_DIR / "rejected_domains.txt"


items = json.loads(RESULTS_FILE.read_text(encoding="utf-8-sig"))
groups: dict[str, list[dict]] = defaultdict(list)
for item in items:
    groups[item["provider"]].append(item)

channels = []
for provider, rows in sorted(groups.items()):
    successes = [row for row in rows if row["status"] == "ok"]
    if not successes:
        continue
    domains = Counter(row["domain"] for row in successes if row["domain"])
    channels.append(
        {
            "provider": provider,
            "attempts": len(rows),
            "successes": len(successes),
            "success_rate": round(len(successes) / len(rows), 4),
            "domains": [
                {"domain": domain, "successes": count}
                for domain, count in domains.most_common()
            ],
        }
    )

catalog = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "attempts_per_provider": 10,
    "concurrency": 2,
    "channels": sorted(channels, key=lambda item: (-item["success_rate"], item["provider"])),
}
WORKING_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

rejected = {
    row["domain"].strip().casefold()
    for row in items
    if row["status"] in {"rejected_temp_email", "known_rejected_temp_email"}
    and row.get("domain")
}
if REJECTED_FILE.exists():
    rejected.update(
        line.strip().casefold()
        for line in REJECTED_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
REJECTED_FILE.write_text(
    "# Domains explicitly rejected by todofor.ai are appended below.\n"
    + "".join(f"{domain}\n" for domain in sorted(rejected)),
    encoding="utf-8",
)

lines = [
    "# MailPoolHub 渠道注册基准",
    "",
    f"- 结果数：{len(items)}",
    f"- 已完成渠道：{sum(len(rows) >= 10 for rows in groups.values())}",
    f"- 可成功渠道：{len(channels)}",
    "- 每渠道目标样本：10",
    "- 并发：2",
    "",
    "| 渠道 | 样本 | 成功 | 状态分布 | 成功域名 |",
    "| --- | ---: | ---: | --- | --- |",
]
for provider, rows in sorted(groups.items()):
    statuses = Counter(row["status"] for row in rows)
    successful_domains = Counter(
        row["domain"] for row in rows if row["status"] == "ok" and row["domain"]
    )
    lines.append(
        f"| `{provider}` | {len(rows)} | {statuses.get('ok', 0)} | "
        f"{', '.join(f'{key}={value}' for key, value in sorted(statuses.items()))} | "
        f"{', '.join(f'{key} ({value})' for key, value in successful_domains.most_common()) or '-'} |"
    )
REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"working_channels={len(channels)} rejected_domains={len(rejected)} results={len(items)}")
