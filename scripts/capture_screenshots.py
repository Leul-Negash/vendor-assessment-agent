#!/usr/bin/env python3
"""Capture the screenshots used in the report.

Starts the API, drives the page with headless Chrome, and saves:

  docs/screenshots/ui-<request>.png    the review console after a run
  docs/screenshots/execution-log.png   the terminal transcript

    python scripts/capture_screenshots.py [--requests VR-007 VR-009]

Requires google-chrome (or chromium) on PATH.
"""

from __future__ import annotations

import argparse
import html
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"

CHROME_CANDIDATES = ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable")


def find_chrome() -> str:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    sys.exit("no chrome/chromium binary found on PATH")


def wait_for_server(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/policy", timeout=2):
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.4)
    sys.exit("the API did not come up in time")


def shoot(chrome: str, url: str, out: Path, height: int = 2600, width: int = 1440) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=2",
            f"--window-size={width},{height}",
            "--virtual-time-budget=6000",
            f"--screenshot={out}",
            url,
        ],
        check=True,
        capture_output=True,
    )
    trim(out)
    print(f"  {out.relative_to(ROOT)}")


def trim(image_path: Path, margin: int = 48) -> None:
    """Drop the empty page background below the content.

    Chrome captures exactly the window it was given, so a short page leaves a
    tail of flat background. Skipped silently if Pillow is not installed.
    """
    try:
        from PIL import Image
    except ImportError:
        return

    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        background = rgb.getpixel((width - 2, height - 2))
        last_content = 0
        for y in range(height - 1, -1, -1):
            row = rgb.crop((0, y, width, y + 1)).getcolors(maxcolors=width * 2) or []
            if any(colour != background for _, colour in row):
                last_content = y
                break
        cut = min(height, last_content + margin)
        if cut < height:
            rgb.crop((0, 0, width, cut)).save(image_path)


def render_log_png(chrome: str, transcript: Path, out: Path, tail: int = 120) -> None:
    """Render the tail of the transcript as a terminal window."""
    lines = transcript.read_text(encoding="utf-8").splitlines()[-tail:]
    body = html.escape("\n".join(lines))
    page = f"""<!doctype html><meta charset="utf-8">
<style>
  body {{ margin: 0; background: #0b0e14; padding: 26px; }}
  .window {{ background: #11141b; border: 1px solid #2a2f3a; border-radius: 8px; overflow: hidden; }}
  .bar {{ display: flex; gap: 7px; align-items: center; padding: 9px 12px; background: #191d26;
          border-bottom: 1px solid #2a2f3a; font: 12px ui-monospace, monospace; color: #9aa3b2; }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; }}
  pre {{ margin: 0; padding: 16px 18px; color: #d6dae2; font: 12.5px/1.5 ui-monospace, "DejaVu Sans Mono", monospace;
         white-space: pre; }}
</style>
<div class="window">
  <div class="bar">
    <span class="dot" style="background:#f85149"></span>
    <span class="dot" style="background:#d29922"></span>
    <span class="dot" style="background:#3fb950"></span>
    <span style="margin-left:8px">leul@training:~/Training/11/vendor-assessment-agent — python scripts/run_batch.py</span>
  </div>
  <pre>{body}</pre>
</div>"""
    page_path = SHOTS / ".execution-log.html"
    page_path.write_text(page, encoding="utf-8")
    # Wide enough that the longest reasoning line is not clipped.
    shoot(chrome, page_path.as_uri(), out, height=min(400 + 26 * len(lines), 16000), width=1820)
    page_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", nargs="+", default=["VR-007", "VR-009"])
    args = parser.parse_args()

    chrome = find_chrome()
    SHOTS.mkdir(parents=True, exist_ok=True)

    # A dedicated run directory so the captures show first submissions rather
    # than the duplicate-guard path left behind by an earlier batch.
    shot_runs = ROOT / "runs" / "screenshots"
    if shot_runs.exists():
        shutil.rmtree(shot_runs)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "VENDOR_AGENT_RUN_DIR": str(shot_runs),
        "VENDOR_AGENT_LOG_DIR": str(ROOT / "logs" / "screenshots"),
    }
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "vendor_agent.api:app",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server()
        print("screenshots:")
        for request_id in args.requests:
            # The page runs the request itself via ?run=, so the capture stays a
            # plain page load rather than a scripted click.
            shoot(chrome, f"{BASE}/?run={request_id}", SHOTS / f"ui-{request_id.lower()}.png")
    finally:
        server.send_signal(signal.SIGINT)
        server.wait(timeout=15)

    transcript = ROOT / "logs" / "execution_log.txt"
    if transcript.exists():
        render_log_png(chrome, transcript, SHOTS / "execution-log.png")
    else:
        print("  (no transcript yet; run scripts/run_batch.py first)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
