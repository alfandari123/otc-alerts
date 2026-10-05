"""Run on the home computer (GitHub's servers are blocked from this lookup):
looks up the OTC Markets security id of every watchlist stock that has none yet and saves it in the
bot's encrypted memory, so the cloud bot can read daily share-count data for it.

    .venv/Scripts/python tools/resolve_ids.py

Needs the encryption key in .state_key and the `gh` login (to see when the bot is between runs).
"""
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.dilution import resolve_id  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPO = "alfandari123/otc-alerts"


def sh(*args, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def wait_between_runs():
    """The bot saves its memory at the end of every run: write only while the next run is still waiting."""
    while True:
        runs = json.loads(sh("gh", "run", "list", "--repo", REPO, "--workflow", "run.yml", "--limit", "3",
                             "--json", "status,createdAt"))
        busy = [r for r in runs if r["status"] in ("in_progress", "queued")]
        waiting = [r for r in runs if r["status"] == "waiting"]
        if not busy and waiting:
            created = datetime.fromisoformat(waiting[0]["createdAt"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - created).total_seconds() < 120:   # the wait lasts 4 minutes
                return
        time.sleep(15)


def main():
    fernet = Fernet((ROOT / ".state_key").read_bytes().strip())
    wait_between_runs()
    sh("git", "fetch", "-q", "origin", "state")
    st = json.loads(fernet.decrypt(subprocess.run(["git", "show", "FETCH_HEAD:state.bin"], cwd=ROOT,
                                                  check=True, capture_output=True).stdout))
    ids = st.setdefault("secid", {})
    added = {}
    for sym in st["watch"]:
        if ids.get(sym):
            continue
        try:
            sid = resolve_id(sym)
        except Exception as e:
            print(f"{sym}: lookup failed ({type(e).__name__})")
            continue
        if sid:
            ids[sym] = added[sym] = sid
        time.sleep(1)
    if not added:
        print("Nothing new to save.")
        return
    st["dil"] = {}   # recompute today's summaries with the precise data
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "state.bin").write_bytes(fernet.encrypt(json.dumps(st, ensure_ascii=False,
                                                                     separators=(",", ":")).encode()))
        sh("git", "init", "-q", "-b", "state", cwd=tmp)
        sh("git", "add", "state.bin", cwd=tmp)
        sh("git", "-c", "user.name=otc-alerts-bot", "-c",
           "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", "state", cwd=tmp)
        sh("git", "push", "-q", "-f", f"https://github.com/{REPO}.git", "state", cwd=tmp)
    print(f"Saved ids for: {', '.join(added)}")


if __name__ == "__main__":
    main()
