"""Where state lives, and whether that place survives a redeploy.

`data/state.json` holds everything the bot remembers: which items it already
bought, sweep progress/cursor, history. On a host (Railway/Render/Fly) the
container filesystem is thrown away on every deploy, so that directory has to be
a mounted volume — otherwise the bot silently starts from scratch.

The only reliable local signal is the device id: a real mount has a different
`st_dev` from the application directory. That's what `analyse()` reports.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

# Set by the hosts themselves, so we only warn when it actually matters.
CONTAINER_HINTS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RENDER",
    "RENDER_SERVICE_ID",
    "FLY_APP_NAME",
    "DYNO",
    "K_SERVICE",
)


def looks_containerised() -> bool:
    return any(os.getenv(key) for key in CONTAINER_HINTS)


def analyse(data_path: str) -> dict[str, Any]:
    """Facts about the state file's location. Never raises."""
    path = Path(data_path).expanduser()
    directory = path.parent

    report: dict[str, Any] = {
        "path": str(path),
        "directory": str(directory),
        "exists": path.exists(),
        "writable": False,
        "separate_mount": False,
        "free_mb": None,
        "recommended_path": "/app/data/state.json",
        "container": looks_containerised(),
    }

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report["error"] = f"cannot create {directory}: {exc}"
        return report

    probe = directory / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["writable"] = True
    except OSError as exc:
        report["error"] = f"cannot write to {directory}: {exc}"

    try:
        report["free_mb"] = shutil.disk_usage(directory).free // (1024 * 1024)
    except OSError:
        pass

    # A volume mounted at the data directory is a different device than the
    # application code sitting next to it.
    try:
        app_dir = Path.cwd()
        if app_dir != directory:
            report["separate_mount"] = os.stat(directory).st_dev != os.stat(app_dir).st_dev
    except OSError:
        pass

    return report


def at_risk(report: dict[str, Any]) -> bool:
    """True when state would be lost on redeploy."""
    if not report.get("container"):
        return False  # a VPS/PC keeps its disk; no volume needed
    return not report.get("separate_mount")


def summary(report: dict[str, Any]) -> str:
    bits = [f"`{report['path']}`"]
    if not report.get("writable"):
        bits.append("❌ not writable")
        return " · ".join(bits)
    if report.get("separate_mount"):
        bits.append("✅ volume")
    elif report.get("container"):
        bits.append("⚠️ **no volume — state is lost on redeploy**")
    else:
        bits.append("✅ local disk")
    if report.get("free_mb") is not None:
        bits.append(f"{report['free_mb']:,} MB free")
    return " · ".join(bits)
