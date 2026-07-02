import requests
import base64

GITHUB_USER  = "chevcheliosllm"
GITHUB_REPO  = "chev-monitor"
GITHUB_FILE  = "index.html"
LOCAL_FILE   = r"C:\ChevTools\webapp\monitor.html"
GITHUB_TOKEN = "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"

def push():
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
