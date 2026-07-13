import requests
import base64
import os

GITHUB_USER  = "ChevCheliosLLM"
GITHUB_REPO  = "chev-trading-system"
REPO_ROOT    = r"C:\ChevTools"
WEBAPP_DIR   = r"C:\ChevTools\webapp"
SECRETS_FILE = r"C:\ChevTools\secrets.local"

# Every file that must exist in the repo for webapp/index.html to actually
# render/function -- css/ and js/ are synced wholesale (whatever's in those
# trees locally), these two are named individually since webapp/ also holds
# files that aren't part of the deployed site (chevportrait.*/icon.svg/
# manifest.json/sw.js aren't referenced by index.html at all; "index 2/3/4.html"
# are stray local duplicates). The whole emoji/ folder is included since JS
# picks a filename from it dynamically (mood/P&L-based), not just the handful
# index.html's own static markup happens to name.
EXTRA_FILES = ["arsenal.png", "chevlogo.png"]
EXTRA_DIRS  = ["emoji"]

def _load_github_token():
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None

GITHUB_TOKEN = _load_github_token()

def _collect_files(local_dir):
    paths = []
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            paths.append(os.path.join(root, name))
    return paths

def _to_remote_path(local_path):
    # Relative to the REPO ROOT (not webapp/) -- chev-trading-system keeps the
    # whole frontend nested under a webapp/ folder alongside dexter.py etc,
    # unlike the old chev-monitor repo (now retired) whose root WAS webapp/.
    rel = os.path.relpath(local_path, REPO_ROOT)
    return rel.replace(os.sep, "/")

def _push_file(local_path, remote_path, headers):
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{remote_path}"
    with open(local_path, "rb") as f:
        content = f.read()

    # Get existing SHA if the file already exists (required for updates; omit for new files)
    resp = requests.get(url, headers=headers)
    payload = {
        "message": "Update dashboard",
        "content": base64.b64encode(content).decode("utf-8"),
    }
    if resp.status_code == 200:
        payload["sha"] = resp.json()["sha"]

    return requests.put(url, headers=headers, json=payload)

def push():
    if not GITHUB_TOKEN:
        print(f"[push_dashboard] No GITHUB_TOKEN found -- set the GITHUB_TOKEN env var, "
              f"or add a GITHUB_TOKEN= line to {SECRETS_FILE}.")
        return

    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    files = [os.path.join(WEBAPP_DIR, "index.html")]
    files += _collect_files(os.path.join(WEBAPP_DIR, "css"))
    files += _collect_files(os.path.join(WEBAPP_DIR, "js"))
    files += [os.path.join(WEBAPP_DIR, name) for name in EXTRA_FILES]
    for d in EXTRA_DIRS:
        files += _collect_files(os.path.join(WEBAPP_DIR, d))

    ok, failed = [], []
    for local_path in files:
        remote_path = _to_remote_path(local_path)
        resp = _push_file(local_path, remote_path, headers)
        if resp.status_code in (200, 201):
            ok.append(remote_path)
        else:
            failed.append((remote_path, resp.status_code, resp.text[:200]))

    print(f"[push_dashboard] {len(ok)} file(s) pushed.")
    if failed:
        print(f"[push_dashboard] {len(failed)} file(s) FAILED:")
        for path, code, text in failed:
            print(f"  {path}: {code} -- {text}")
    else:
        print("[push_dashboard] Done -- live at https://chevcheliosllm.github.io/chev-trading-system/webapp/index.html")

if __name__ == "__main__":
    push()
