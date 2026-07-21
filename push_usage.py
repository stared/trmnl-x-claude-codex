# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Push Claude Code + Codex usage to a TRMNL private plugin webhook.

Data sources:
- Claude rate limits + plan: undocumented https://api.anthropic.com/api/oauth/usage
  endpoint; OAuth token + subscription tier from the macOS Keychain
  ("Claude Code-credentials").
- Codex rate limits: `codex app-server` JSON-RPC (account/rateLimits/read).
- Cost per day (API-equivalent $, all detected agents) and top Claude projects:
  ccusage via `pnpm dlx`, plus ~/.claude/projects/ for session->project mapping.

Config: TRMNL_WEBHOOK_URL env var, or config.json next to this file:
        {"webhook_url": "https://trmnl.com/api/custom_plugins/YOUR-PLUGIN-UUID"}

Usage:  uv run push_usage.py [--dry-run]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
COST_DAYS = 7
TOP_PROJECTS = 6
CHART_MAX_PX = 230  # tallest bar in the template's chart area


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def fmt_reset(dt: datetime) -> str:
    """Compact local-time label for a reset moment, e.g. '19:09' or 'Mon 27 Jul, 10:19'."""
    local = dt.astimezone()
    if dt - datetime.now(timezone.utc) < timedelta(hours=24):
        return local.strftime("%H:%M")
    return local.strftime("%a %d %b, %H:%M")


def keychain_creds() -> dict:
    creds_json = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return json.loads(creds_json)["claudeAiOauth"]


def plan_label(creds: dict) -> str:
    """'max' + 'default_claude_max_20x' -> 'Max 20x'."""
    sub = (creds.get("subscriptionType") or "").capitalize()
    tier = creds.get("rateLimitTier") or ""
    mult = tier.rsplit("_", 1)[-1]
    if mult.endswith("x") and mult[:-1].isdigit():
        return f"{sub} {mult}".strip()
    return sub


def get_claude_usage() -> dict:
    creds = keychain_creds()
    req = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    tiles = []
    for limit in data.get("limits", []):
        pct = round(limit.get("percent") or 0)
        reset = fmt_reset(datetime.fromisoformat(limit["resets_at"]))
        kind = limit.get("kind")
        if kind == "session":
            tiles.append({"win": "Session 5h", "pct": pct, "reset": reset})
        elif kind == "weekly_all":
            tiles.append({"win": "Weekly", "pct": pct, "reset": reset})
        elif kind == "weekly_scoped":
            model = ((limit.get("scope") or {}).get("model") or {}).get("display_name") or "?"
            tiles.append({"win": f"Weekly · {model}", "pct": pct, "reset": reset})
    who = f"Claude Code · {plan_label(creds)}".rstrip(" ·")
    return {"claude_group": {"who": who, "tiles": tiles}}


def get_codex_usage() -> dict:
    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    try:
        send({"method": "initialize", "id": 0,
              "params": {"clientInfo": {"name": "trmnl-usage", "title": "TRMNL Usage", "version": "1.0"}}})
        time.sleep(0.5)
        send({"method": "account/rateLimits/read", "id": 1, "params": {}})

        result = None
        deadline = time.time() + 15
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 1:
                result = msg.get("result")
                break
    finally:
        proc.kill()

    if not result:
        raise RuntimeError("no rate-limit response from codex app-server")

    limits = result["rateLimits"]
    tiles = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not window:
            continue
        mins = window.get("windowDurationMins") or 0
        win = "Session 5h" if mins == 300 else "Weekly" if mins == 10080 else f"{mins // 60}h"
        resets = datetime.fromtimestamp(window["resetsAt"], tz=timezone.utc)
        tiles.append({
            "win": win,
            "pct": round(window["usedPercent"]),
            "reset": fmt_reset(resets),
        })
    plan = (limits.get("planType") or "").capitalize()
    who = f"Codex · {plan}".rstrip(" ·")
    return {"codex_group": {"who": who, "tiles": tiles}}


def run_ccusage(*args: str) -> dict:
    since = (datetime.now() - timedelta(days=COST_DAYS - 1)).strftime("%Y%m%d")
    out = subprocess.run(
        ["pnpm", "dlx", "ccusage", *args, "--json", "--since", since],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    return json.loads(out)


def get_daily_costs() -> dict:
    """API-equivalent $ per day: Claude Code vs other agents ccusage detects.

    Note: ccusage reports $0 for Codex (no pricing for ChatGPT-plan usage),
    so 'other' is effectively opencode & co. until that changes.
    """
    all_rows = run_ccusage("daily").get("daily", [])
    claude_rows = run_ccusage("claude", "daily").get("daily", [])
    total_by_day = {r["period"]: r.get("totalCost") or 0 for r in all_rows}
    claude_by_day = {r["date"]: r.get("totalCost") or 0 for r in claude_rows}

    days = []
    today = datetime.now().date()
    for i in range(COST_DAYS - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        total = total_by_day.get(day, 0)
        claude = min(claude_by_day.get(day, 0), total)
        days.append({
            "d": (today - timedelta(days=i)).strftime("%a")[:2],
            "c": round(total),
            "claude": claude,
            "other": total - claude,
        })
    max_cost = max((d["c"] for d in days), default=0) or 1
    for d in days:
        d["h1"] = round(CHART_MAX_PX * d.pop("claude") / max_cost)
        d["h2"] = round(CHART_MAX_PX * d.pop("other") / max_cost)
    return {"cost_days": days, "cost_week": round(sum(d["c"] for d in days))}


def project_name_by_session() -> dict[str, str]:
    """Map session UUID -> project basename, reading the real cwd from each JSONL."""
    mapping: dict[str, str] = {}
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        name = None
        for jsonl in project_dir.glob("*.jsonl"):
            if name is None:
                try:
                    with jsonl.open() as f:
                        for line in f:
                            cwd = json.loads(line).get("cwd")
                            if cwd:
                                name = Path(cwd).name
                                break
                except (OSError, json.JSONDecodeError):
                    pass
                if name is None:
                    name = project_dir.name.rsplit("-", 1)[-1]
            mapping[jsonl.stem] = name
    return mapping


def get_top_projects() -> dict:
    rows = run_ccusage("session").get("session", [])
    names = project_name_by_session()
    totals: dict[str, float] = {}
    for r in rows:
        name = names.get(r.get("period", ""))
        if name:  # skips non-Claude agents' sessions
            totals[name] = totals.get(name, 0) + (r.get("totalCost") or 0)
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:TOP_PROJECTS]
    max_cost = top[0][1] if top else 1
    return {"top_projects": [
        {"name": n[:24], "c": round(c), "w": round(100 * c / max_cost)} for n, c in top
    ]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print payload, skip webhook POST")
    args = parser.parse_args()

    webhook_url = os.environ.get("TRMNL_WEBHOOK_URL")
    if not webhook_url:
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            webhook_url = json.loads(config_path.read_text()).get("webhook_url")
    if not webhook_url and not args.dry_run:
        log("ERROR: no webhook URL — set TRMNL_WEBHOOK_URL or create config.json")
        return 1

    merge_vars: dict = {
        "updated_at": datetime.now().astimezone().strftime("%a %H:%M"),
    }

    for fetch, ok_flag in [
        (get_claude_usage, "claude_ok"),
        (get_codex_usage, "codex_ok"),
        (get_daily_costs, "costs_ok"),
        (get_top_projects, "projects_ok"),
    ]:
        try:
            merge_vars.update(fetch())
            merge_vars[ok_flag] = True
        except Exception as e:
            log(f"{ok_flag[:-3]} fetch failed: {e}")
            merge_vars[ok_flag] = False

    # provider groups for the template (header stated once per group)
    merge_vars["gauge_groups"] = [
        g for g in (merge_vars.pop("claude_group", None), merge_vars.pop("codex_group", None)) if g
    ]

    payload = {"merge_variables": merge_vars}
    body = json.dumps(payload)
    log(f"payload ({len(body)} bytes): {body}")

    if args.dry_run:
        return 0

    req = urllib.request.Request(
        webhook_url,
        data=body.encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "trmnl-usage/1.0 (curl-compatible)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        log(f"webhook response: {resp.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
