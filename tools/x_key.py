"""Reuven runs this himself (Desktop "מפתח טוויטר לבוט.bat"): he copies each cookie value in Chrome DevTools and
presses Enter here - the value is read from the clipboard (no pasting) and goes straight to the GitHub secret
X_COOKIES. Nothing is printed or saved on disk; the clipboard is cleared at the end."""
import subprocess
import tkinter


def clip():
    tk = tkinter.Tk()
    tk.withdraw()
    try:
        return tk.clipboard_get().strip()
    except tkinter.TclError:
        return ""
    finally:
        tk.destroy()


def ask(name):
    while True:
        input(f"\n>>> Copy the value of {name} in Chrome (Ctrl+C), then come back here and press Enter ")
        v = clip()
        if len(v) >= 20 and " " not in v:
            print(f"OK - got {name} ({len(v)} characters)")
            return v
        print("Did not find a copied value. Try again: double-click the value in the Value column, Ctrl+C.")


auth = ask("auth_token")
ct0 = ask("ct0")
if auth == ct0:
    print("\nThe two values are the same - ct0 was not copied. Nothing was sent. Run again.")
else:
    r = subprocess.run(["gh", "secret", "set", "X_COOKIES", "--repo", "alfandari123/otc-alerts"],
                       input=f"auth_token={auth}; ct0={ct0}", text=True, capture_output=True)
    subprocess.run("echo off | clip", shell=True)
    print("\nDONE - the bot has the X key. Tell Claude: muchan" if r.returncode == 0
          else "\nFAILED - send Claude a photo of this window:\n" + (r.stderr or "")[:300])
input("\nPress Enter to close")
