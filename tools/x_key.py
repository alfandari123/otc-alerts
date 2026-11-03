"""Reuven runs this himself (X_KEY.bat): pastes the bot X account's two cookies, they go straight to the GitHub
secret X_COOKIES. Nothing is printed or saved on disk."""
import getpass
import subprocess

print("Paste the value of auth_token, then press Enter (it stays hidden):")
auth = getpass.getpass("auth_token: ").strip()
print("Paste the value of ct0, then press Enter (it stays hidden):")
ct0 = getpass.getpass("ct0: ").strip()
if len(auth) < 20 or len(ct0) < 20:
    print("\nToo short - something was not pasted. Nothing was sent. Run again.")
else:
    r = subprocess.run(["gh", "secret", "set", "X_COOKIES", "--repo", "alfandari123/otc-alerts"],
                       input=f"auth_token={auth}; ct0={ct0}", text=True, capture_output=True)
    print("\nDONE - the bot has the X key. Tell Claude: muchan" if r.returncode == 0
          else "\nFAILED - send Claude a photo of this window:\n" + (r.stderr or "")[:300])
input("\nPress Enter to close")
