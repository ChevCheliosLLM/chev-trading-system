import requests
import base64
import os

GITHUB_USER  = "chevcheliosllm"
GITHUB_REPO  = "chev-monitor"
GITHUB_FILE  = "index.html"
LOCAL_FILE   = r"C:\ChevTools\webapp\index.html"
SECRETS_FILE = r"C:\ChevTools\secrets.local"

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

def push():
    if not GITHUB_TOKEN:
        print(f"[push_dashboard] No GITHUB_TOKEN found -- set the GITHUB_TOKEN env var, "
              f"or add a GITHUB_TOKEN= line to {SECRETS_FILE}.")
        return
    url     = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    with open(LOCAL_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # Get existing SHA if the file already exists (required for updates; omit for new files)
    resp = requests.get(url, headers=headers)
    payload = {
        "message": "Update dashboard",
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
    }
    if resp.status_code == 200:
        payload["sha"] = resp.json()["sha"]

    resp = requests.put(url, headers=headers, json=payload)

    if resp.status_code in (200, 201):
        print("[push_dashboard] Done — live at https://chevcheliosllm.github.io/chev-monitor/")
    else:
        print(f"[push_dashboard] Failed: {resp.status_code} — {resp.text[:300]}")

if __name__ == "__main__":
    push()
