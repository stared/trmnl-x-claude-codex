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

CONFIG_PATH = Path(__file__).parent / "config.json"
CONFIG: dict = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
COST_DAYS = int(CONFIG.get("cost_days", 7))
TOP_PROJECTS = int(CONFIG.get("top_projects", 6))
CHART_MAX_PX = int(CONFIG.get("chart_max_px", 230))  # tallest chart bar


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


def run_ccusage(*args: str, since: str | None = None, until: str | None = None) -> dict:
    if since is None:
        since = (datetime.now() - timedelta(days=COST_DAYS - 1)).strftime("%Y%m%d")
    cmd = ["pnpm", "dlx", "ccusage", *args, "--json", "--since", since]
    if until:
        cmd += ["--until", until]
    out = subprocess.run(
        cmd, capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    return json.loads(out)


def get_daily_costs() -> dict:
    """API-equivalent $ per day, stacked per agent (Claude, Codex, opencode, ...).

    Top NAMED_AGENTS by weekly cost get their own shade and legend entry;
    the remainder is lumped as 'other'.
    """
    NAMED_AGENTS = 3
    SHADES = ["#000", "#777", "#aaa"]  # by rank; lump uses LUMP_SHADE
    LUMP_SHADE = "#ccc"

    rows = run_ccusage("daily", "--by-agent").get("daily", [])
    cost: dict[str, dict[str, float]] = {}  # agent -> day -> $
    for r in rows:
        for a in r.get("agents", []):
            c = sum(m.get("cost") or 0 for m in a.get("modelBreakdowns", []))
            if c > 0:
                agent_costs = cost.setdefault(a.get("agent") or "?", {})
                agent_costs[r["period"]] = agent_costs.get(r["period"], 0) + c

    ranked = sorted(cost, key=lambda a: -sum(cost[a].values()))
    ranked.sort(key=lambda a: a != "claude")  # Claude first, keep cost order after
    named, lumped = ranked[:NAMED_AGENTS], ranked[NAMED_AGENTS:]

    today = datetime.now().date()
    dates = [(today - timedelta(days=i)) for i in range(COST_DAYS - 1, -1, -1)]
    per_day_totals = {
        d.isoformat(): sum(cost[a].get(d.isoformat(), 0) for a in ranked) for d in dates
    }
    max_cost = max(per_day_totals.values(), default=0) or 1

    days = []
    for d in dates:
        key = d.isoformat()
        segs = [
            {"h": round(CHART_MAX_PX * cost[a].get(key, 0) / max_cost), "s": SHADES[i]}
            for i, a in enumerate(named)
        ] + [{
            "h": round(CHART_MAX_PX * sum(cost[a].get(key, 0) for a in lumped) / max_cost),
            "s": LUMP_SHADE,
        }]
        days.append({
            "d": d.strftime("%a")[:2],
            "c": round(per_day_totals[key]),
            "seg": [s for s in segs if s["h"] > 0],
        })

    legend = [
        {"label": "Claude Code" if a == "claude" else a, "s": SHADES[i]}
        for i, a in enumerate(named)
    ]
    if lumped:
        legend.append({"label": "other", "s": LUMP_SHADE})
    return {
        "cost_days": days,
        "cost_week": round(sum(per_day_totals.values())),
        "cost_legend": legend,
    }


def scan_projects() -> tuple[dict[str, str], dict[str, list[Path]]]:
    """Map session UUID -> project basename (real cwd read from the JSONL),
    and project basename -> its JSONL files."""
    by_session: dict[str, str] = {}
    files_by_name: dict[str, list[Path]] = {}
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        name = None
        jsonls = list(project_dir.glob("*.jsonl"))
        for jsonl in jsonls:
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
            by_session[jsonl.stem] = name
        if name:
            files_by_name.setdefault(name, []).extend(jsonls)
    return by_session, files_by_name


def day_series(files: list[Path]) -> list[float]:
    """Cost-weighted token activity per local day (oldest first) from JSONLs.

    Relative pricing weights (input=1): output 5x, cache read 0.1x,
    cache write 1.25x. Sparklines are scaled to each project's own peak,
    so relative weights are as informative as exact dollars.
    """
    today = datetime.now().date()
    start = today - timedelta(days=COST_DAYS - 1)
    series = [0.0] * COST_DAYS
    for path in files:
        try:
            if datetime.fromtimestamp(path.stat().st_mtime).date() < start:
                continue
            with path.open() as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    usage = (entry.get("message") or {}).get("usage")
                    ts = entry.get("timestamp")
                    if not usage or not ts:
                        continue
                    day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date()
                    idx = (day - start).days
                    if 0 <= idx < COST_DAYS:
                        series[idx] += (
                            (usage.get("input_tokens") or 0)
                            + 5.0 * (usage.get("output_tokens") or 0)
                            + 0.1 * (usage.get("cache_read_input_tokens") or 0)
                            + 1.25 * (usage.get("cache_creation_input_tokens") or 0)
                        )
        except OSError:
            continue
    return series


def get_top_projects() -> dict:
    """Top projects by 7d cost (ccusage), each with a per-day sparkline series.

    ccusage session totals aren't range-scoped per day, so the sparkline
    comes from the project's own JSONLs via a price-weighted token proxy.
    """
    by_session, files_by_name = scan_projects()
    rows = run_ccusage("session").get("session", [])
    totals: dict[str, float] = {}
    for r in rows:
        name = by_session.get(r.get("period", ""))
        if name:  # skips non-Claude agents' sessions
            totals[name] = totals.get(name, 0) + (r.get("totalCost") or 0)

    top = sorted(totals.items(), key=lambda kv: -kv[1])[:TOP_PROJECTS]
    projects = []
    for name, cost in top:
        series = day_series(files_by_name.get(name, []))
        peak = max(series) or 1
        projects.append({
            "name": name[:24],
            "c": round(cost),
            "s": [round(100 * v / peak) for v in series],  # scaled to own peak
        })
    return {"top_projects": projects}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print payload, skip webhook POST")
    args = parser.parse_args()

    webhook_url = os.environ.get("TRMNL_WEBHOOK_URL") or CONFIG.get("webhook_url")
    if not webhook_url and not args.dry_run:
        log("ERROR: no webhook URL — set TRMNL_WEBHOOK_URL or create config.json")
        return 1

    merge_vars: dict = {
        "updated_at": datetime.now().astimezone().strftime("%a %H:%M"),
    }

    errors: list[str] = []
    for fetch, ok_flag in [
        (get_claude_usage, "claude_ok"),
        (get_codex_usage, "codex_ok"),
        (get_daily_costs, "costs_ok"),
        (get_top_projects, "projects_ok"),
    ]:
        section = ok_flag[:-3]
        try:
            merge_vars.update(fetch())
            merge_vars[ok_flag] = True
        except Exception as e:
            log(f"{section} fetch failed: {e}")
            merge_vars[ok_flag] = False
            errors.append(f"{section}: {str(e)[:80]}")
    merge_vars["errors"] = errors  # rendered as a warning strip on the device

    # provider groups for the template (header stated once per group)
    merge_vars["gauge_groups"] = [
        g for g in (merge_vars.pop("claude_group", None), merge_vars.pop("codex_group", None)) if g
    ]

    payload = {"merge_variables": merge_vars}
    body = json.dumps(payload)
    log(f"payload ({len(body)} bytes): {body}")

    if args.dry_run:
        return 1 if errors else 0

    req = urllib.request.Request(
        webhook_url,
        data=body.encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "trmnl-usage/1.0 (curl-compatible)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"webhook response: {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code == 429:  # TRMNL cap: 12 pushes/hour — next run will catch up
            log("webhook rate-limited (429), data not delivered this round")
            return 1
        raise
    # non-zero when any section failed, so launchctl/logs show degraded runs
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
