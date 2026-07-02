import tkinter as tk
import threading
import subprocess
import webbrowser
import requests
import time
import sys
from PIL import Image, ImageTk, ImageDraw

DEXTER_SCRIPT    = r"C:\ChevTools\dexter.py"
TELEGRAM_SCRIPT  = r"C:\ChevTools\telegram_listener.py"
PORTRAIT         = r"C:\ChevTools\webapp\chevportrait.png"
TERMINAL         = "http://localhost:8080"
WEBUI            = "http://localhost:3000"
MONITOR_URL      = "https://chevcheliosllm.github.io/chev-monitor/"
DOCKER_DESKTOP   = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
CHROME_EXE       = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
NGROK_EXE        = r"C:\Users\kevin\OneDrive\Desktop\ngrok.exe"
NGROK_API        = "http://localhost:4040/api/tunnels"

BG     = "#131722"
CARD   = "#1c2030"
BORDER = "#1e222d"
GREEN  = "#089981"
RED    = "#f23645"
GOLD   = "#d4af37"
WHITE  = "#d1d4dc"
DIM    = "#5d6068"


def _check(url, timeout=3):
    try:
        return requests.get(url, timeout=timeout).status_code < 500
    except Exception:
        return False


def _check_docker():
    try:
        r = subprocess.run(
            ["docker", "ps"],
            capture_output=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def _get_ngrok_url():
    try:
        r = requests.get(NGROK_API, timeout=2)
        tunnels = r.json().get("tunnels", [])
        for t in tunnels:
            if t.get("proto") == "https":
                return t["public_url"]
        if tunnels:
            return tunnels[0]["public_url"]
    except Exception:
        pass
    return None


def _make_circle_portrait(path, size=128):
    img = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, mask=mask)
    return result


class ChevLauncher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ChevTools")
        self.root.geometry("400x980")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        try:
            icon = _make_circle_portrait(PORTRAIT, 32)
            self.root.iconphoto(True, ImageTk.PhotoImage(icon))
        except Exception:
            pass

        self._dots   = {}
        self._status = {
            "docker": False, "webui": False, "dexter": False,
            "gemini": False, "telegram": False, "ngrok": False,
        }
        self._ngrok_url = None

        self._build_ui()
        threading.Thread(target=self._status_loop, daemon=True).start()
        self.root.mainloop()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        try:
            img = _make_circle_portrait(PORTRAIT, 110)
            self._photo = ImageTk.PhotoImage(img)
            tk.Label(self.root, image=self._photo, bg=BG).pack(pady=(18, 6))
        except Exception:
            tk.Label(self.root, text="⚡", font=("Segoe UI", 48), bg=BG, fg=GOLD).pack(pady=(18, 6))

        tk.Label(self.root, text="ChevTools",
                 font=("Segoe UI", 20, "bold"), bg=BG, fg=WHITE).pack()
        tk.Label(self.root, text="Trading Intelligence Suite",
                 font=("Segoe UI", 9), bg=BG, fg=DIM).pack(pady=(2, 14))

        # Status cards
        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x", padx=28)

        for key, name, desc, action in [
            ("docker",   "Docker",    "Container runtime · runs Open WebUI",         self._start_docker),
            ("webui",    "Open WebUI","Chev's interface · localhost:3000",            lambda: webbrowser.open(WEBUI)),
            ("dexter",   "Dexter",    "Trading bot · start in VS Code terminal",      None),
            ("gemini",   "Ollama",    "Chev's brain · qwen2.5:32b · localhost:11434", lambda: webbrowser.open("http://localhost:11434")),
            ("telegram", "Telegram",  "Chev in Telegram · start in VS Code terminal", None),
            ("ngrok",    "Ngrok",     "Charts tunnel · localhost:8080 → public URL",  self._start_ngrok),
        ]:
            self._service_card(cards, key, name, desc, action)

        # Main button
        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=28, pady=(14, 0))

        self._launch_btn = tk.Button(
            btns, text="OPEN ALL",
            font=("Segoe UI", 13, "bold"),
            bg="#1e3a5f", fg=WHITE, relief="flat", cursor="hand2",
            pady=13, activebackground="#265080", activeforeground=WHITE,
            command=self._launch_everything,
        )
        self._launch_btn.pack(fill="x", pady=(0, 6))

        # Quick-open buttons — row 1
        row1 = tk.Frame(btns, bg=BG)
        row1.pack(fill="x", pady=(0, 4))

        for label, url, pl, pr in [
            ("Monitor",   MONITOR_URL, 0, 3),
            ("Charts",    TERMINAL,    3, 3),
            ("Open WebUI", WEBUI,      3, 0),
        ]:
            u = url
            tk.Button(row1, text=label,
                      font=("Segoe UI", 9), bg=CARD, fg=WHITE, relief="flat",
                      cursor="hand2", pady=7,
                      command=lambda u=u: webbrowser.open(u)
                      ).pack(side="left", fill="x", expand=True, padx=(pl, pr))

        # Quick-open buttons — row 2
        row2 = tk.Frame(btns, bg=BG)
        row2.pack(fill="x", pady=(0, 4))

        self._ngrok_btn = tk.Button(
            row2, text="Ngrok URL (checking…)",
            font=("Segoe UI", 9), bg=CARD, fg=GOLD, relief="flat",
            cursor="hand2", pady=7,
            command=self._open_ngrok_url,
        )
        self._ngrok_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))

        tk.Button(
            row2, text="Ngrok Dashboard",
            font=("Segoe UI", 9), bg=CARD, fg=DIM, relief="flat",
            cursor="hand2", pady=7,
            command=lambda: webbrowser.open("http://localhost:4040"),
        ).pack(side="left", fill="x", expand=True, padx=(3, 0))

        # ── VS Code Terminal Checklist ───────────────────────────────────────
        chk_outer = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                             highlightthickness=1)
        chk_outer.pack(fill="x", padx=28, pady=(14, 0))

        hdr = tk.Frame(chk_outer, bg=CARD)
        hdr.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(hdr, text="VS CODE TERMINAL CHECKLIST",
                 font=("Segoe UI", 8, "bold"), bg=CARD, fg=GOLD).pack(side="left")
        tk.Label(hdr, text="tick when running",
                 font=("Segoe UI", 7), bg=CARD, fg=DIM).pack(side="right")

        self._chk_vars = {}
        terminal_items = [
            ("dexter",    "python dexter.py",
             "Main API · engines.py + patterns.py auto-load with this"),
            ("telegram",  "python telegram_listener.py",
             "Telegram bot · Dexter in your Telegram"),
            ("ollama",    "ollama serve",
             "Chev's brain · only if Ollama dot is red"),
        ]
        for key, cmd, note in terminal_items:
            var = tk.BooleanVar(value=False)
            self._chk_vars[key] = var
            row = tk.Frame(chk_outer, bg=CARD)
            row.pack(fill="x", padx=6, pady=2)

            cb = tk.Checkbutton(row, variable=var, bg=CARD, fg=WHITE,
                                selectcolor="#0d3320", activebackground=CARD,
                                cursor="hand2", relief="flat", bd=0)
            cb.pack(side="left", padx=(4, 0))

            inner = tk.Frame(row, bg=CARD)
            inner.pack(side="left", fill="x", expand=True, padx=(2, 0))
            tk.Label(inner, text=cmd, font=("Courier New", 8, "bold"),
                     bg=CARD, fg=WHITE).pack(anchor="w")
            tk.Label(inner, text=note, font=("Segoe UI", 7),
                     bg=CARD, fg=DIM).pack(anchor="w")

            def _copy(c=cmd):
                self.root.clipboard_clear()
                self.root.clipboard_append(c)
                self._bar.config(text=f"Copied to clipboard: {c}")
            tk.Button(row, text="⎘", font=("Segoe UI", 10), bg=CARD, fg=DIM,
                      relief="flat", cursor="hand2", pady=0, padx=6,
                      activebackground=CARD, activeforeground=WHITE,
                      command=_copy).pack(side="right", padx=(0, 4))

        # Module note (never run these directly)
        note_row = tk.Frame(chk_outer, bg=CARD)
        note_row.pack(fill="x", padx=10, pady=(4, 8))
        tk.Label(note_row,
                 text="engines.py · patterns.py — imported by dexter.py, never run directly",
                 font=("Segoe UI", 7), bg=CARD, fg=DIM, wraplength=330,
                 justify="left").pack(anchor="w")

        self._bar = tk.Label(self.root, text="Checking services…",
                             font=("Segoe UI", 9), bg=BG, fg=DIM, wraplength=360)
        self._bar.pack(pady=(10, 0))

    def _service_card(self, parent, key, name, desc, action=None):
        frame = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        frame.pack(fill="x", pady=3)
        if action:
            frame.config(cursor="hand2")
            frame.bind("<Button-1>", lambda e, a=action: a())
            frame.bind("<Enter>", lambda e: frame.config(highlightbackground=DIM))
            frame.bind("<Leave>", lambda e: frame.config(highlightbackground=BORDER))

        dot = tk.Label(frame, text="●", font=("Segoe UI", 16),
                       bg=CARD, fg=RED, width=2)
        dot.pack(side="left", padx=(10, 0), pady=9)
        if action:
            dot.bind("<Button-1>", lambda e, a=action: a())
        self._dots[key] = dot

        info = tk.Frame(frame, bg=CARD)
        info.pack(side="left", padx=10, pady=6)
        tk.Label(info, text=name, font=("Segoe UI", 11, "bold"),
                 bg=CARD, fg=WHITE).pack(anchor="w")
        tk.Label(info, text=desc, font=("Segoe UI", 8),
                 bg=CARD, fg=DIM).pack(anchor="w")
        if action:
            info.bind("<Button-1>", lambda e, a=action: a())
            for child in info.winfo_children():
                child.bind("<Button-1>", lambda e, a=action: a())

    # ── Individual service actions ────────────────────────────────────────────

    def _start_docker(self):
        if self._status["docker"]:
            self._bar.config(text="Docker is already running.")
            return
        try:
            subprocess.Popen([DOCKER_DESKTOP], creationflags=subprocess.CREATE_NO_WINDOW)
            self._bar.config(text="Docker opening — wait ~20s, then click again.")
        except Exception as e:
            self._bar.config(text=f"Couldn't open Docker: {e}")

    def _start_ngrok(self):
        if self._status["ngrok"]:
            self._bar.config(text=f"Ngrok running → {self._ngrok_url or 'fetching URL…'}")
            return
        try:
            subprocess.Popen(
                [NGROK_EXE, "http", "8080"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._bar.config(text="Ngrok starting — wait a few seconds…")
        except Exception as e:
            self._bar.config(text=f"Couldn't start Ngrok: {e}")

    def _open_ngrok_url(self):
        url = self._ngrok_url or _get_ngrok_url()
        if url:
            webbrowser.open(url)
        else:
            self._bar.config(text="Ngrok not running — click the Ngrok service card to start it.")

    # ── Logic ────────────────────────────────────────────────────────────────

    def _launch_everything(self):
        if not self._status["docker"]:
            self._start_docker()
            return

        # Start ngrok if not already up
        if not self._status["ngrok"]:
            self._start_ngrok()

        ngrok_url = self._ngrok_url or TERMINAL
        try:
            subprocess.Popen(
                [CHROME_EXE, "--new-window", MONITOR_URL, ngrok_url, WEBUI],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            webbrowser.open(MONITOR_URL)
            webbrowser.open(ngrok_url)
            webbrowser.open(WEBUI)

    def _check_telegram(self):
        try:
            r2 = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return "telegram_listener" in r2.stdout
        except Exception:
            return False

    def _status_loop(self):
        while True:
            docker   = _check_docker()
            webui    = _check(WEBUI)
            dexter   = _check(f"{TERMINAL}/api/trades")
            gemini   = _check("http://localhost:11434", timeout=4)
            telegram = self._check_telegram()
            ngrok    = _check(NGROK_API, timeout=2)
            ngrok_url = _get_ngrok_url() if ngrok else None

            self._status = {
                "docker": docker, "webui": webui, "dexter": dexter,
                "gemini": gemini, "telegram": telegram, "ngrok": ngrok,
            }
            self._ngrok_url = ngrok_url
            self.root.after(0, self._apply_status)
            time.sleep(5)

    def _apply_status(self):
        for key in ("docker", "webui", "dexter", "gemini", "telegram", "ngrok"):
            self._set_dot(key, self._status[key])

        all_up = all(self._status.values())
        if all_up:
            self._launch_btn.config(bg="#0d3320", fg=GREEN)
        else:
            self._launch_btn.config(bg="#1e3a5f", fg=WHITE)

        # Update ngrok button label with live URL
        if self._ngrok_url:
            short = self._ngrok_url.replace("https://", "")
            self._ngrok_btn.config(text=short, fg=GREEN)
        else:
            self._ngrok_btn.config(text="Ngrok offline", fg=DIM)

        self._bar.config(text=f"Last checked: {time.strftime('%H:%M:%S')}")

    def _set_dot(self, key, online):
        self._dots[key].config(fg=GREEN if online else RED)


if __name__ == "__main__":
    ChevLauncher()
