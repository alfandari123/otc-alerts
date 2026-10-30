"""Leave a command for the bot (applied on its next run, confirmed in Telegram).

    python tools/bot_cmd.py add TLHC FMBV
    python tools/bot_cmd.py remove ABCD EFGH --note "..."
    python tools/bot_cmd.py grpseed          # group scoreboard from ~/.otc-secrets/group_seed.json (group_backtest.py)
    python tools/bot_cmd.py secid            # only the OTC Markets ids from that file (silent, no Telegram message)

The commands are encrypted with the bot's key (.state_key) and pushed to the `inbox` branch - the repo is public,
so tickers never appear in clear text. Old commands stay in the file; the bot remembers which ones it applied.
"""
import argparse
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
REPO = "alfandari123/otc-alerts"


def sh(*args, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("op", choices=["add", "remove", "grpseed", "secid"])
    ap.add_argument("syms", nargs="*")
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    fernet = Fernet((ROOT / ".state_key").read_bytes().strip())
    cmds = []
    if subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", "inbox"], cwd=ROOT,
                      capture_output=True).returncode == 0:
        sh("git", "fetch", "-q", "origin", "inbox")
        raw = subprocess.run(["git", "show", "FETCH_HEAD:inbox.bin"], cwd=ROOT, check=True, capture_output=True).stdout
        cmds = json.loads(fernet.decrypt(raw))
    cmd = {"id": uuid.uuid4().hex, "op": a.op, "syms": [s.upper() for s in a.syms], "note": a.note}
    seed = Path.home() / ".otc-secrets" / "group_seed.json"
    if a.op == "grpseed":
        cmd.update(json.loads(seed.read_text(encoding="utf-8")))
    elif a.op == "secid":
        cmd["secid"] = json.loads(seed.read_text(encoding="utf-8"))["secid"]
    cmds = cmds[-50:] + [cmd]
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "inbox.bin").write_bytes(fernet.encrypt(json.dumps(cmds).encode()))
        sh("git", "init", "-q", "-b", "inbox", cwd=tmp)
        sh("git", "add", "inbox.bin", cwd=tmp)
        sh("git", "-c", "user.name=otc-alerts-bot", "-c",
           "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", "inbox", cwd=tmp)
        sh("git", "push", "-q", "-f", f"https://github.com/{REPO}.git", "inbox", cwd=tmp)
    print(f"queued: {a.op} {' '.join(a.syms)} ({len(cmds)} commands in the inbox)")


if __name__ == "__main__":
    main()
