import tkinter as tk
import threading
import subprocess
import webbrowser
import requests
import time
import sys
import os
from PIL import Image, ImageTk, ImageDraw

DEXTER_SCRIPT    = r"C:\ChevTools\dexter.py"
TELEGRAM_SCRIPT  = r"C:\ChevTools\telegram_listener.py"
PORTRAIT         = r"C:\ChevTools\webapp\chevportrait.png"
TERMINAL         = "http://localhost:8080"
WEBUI            = "http://localhost:3000"
MONITOR_URL      = "https://chevcheliosllm.github.io/chev-monitor/"
DOCKER_DESKTOP   = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
CHROME_EXE       = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CLOUDFLARED_EXE  = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
CHEV_BRAIN_MODELFILE = r"C:\ChevTools\ChevBrain.modelfile"
CHEV_BRAIN_NAME      = "chev-32b"
CHEV_BRAIN_LEARN_MODELFILE = r"C:\ChevTools\ChevBrain-learn.modelfile"
CHEV_BRAIN_LEARN_NAME      = "chev-32b-learn"
LOG_DIR              = r"C:\ChevTools\logs"

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
            "gemini": False, "telegram": False, "brain": False,
        }
        self._brain_built = False
        self._procs = {}
        self._tunnel_proc = None
        self._tunnel_url  = None

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
            ("webui",    "Open WebUI","Chev's interface · localhost:3000",           lambda: webbrowser.open(WEBUI)),
            ("dexter",   "Dexter",    "Trading bot · click to start",                self._start_dexter),
            ("gemini",   "Ollama",    "Chev's brain · click to start",               self._start_ollama),
            ("telegram", "Telegram",  "Chev in Telegram · click to start",           self._start_telegram),
            ("brain",    "Chev Brain","Corrected 32B (chev-32b + chev-32b-learn) · one-click build",   self._build_brain),
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

        self._brain_btn = tk.Button(
            row2, text="Build Chev Brain",
            font=("Segoe UI", 9), bg=CARD, fg=GOLD, relief="flat",
            cursor="hand2", pady=7,
            command=self._build_brain,
        )
        self._brain_btn.pack(side="left", fill="x", expand=True, padx=(0, 2))

        tk.Button(
            row2, text="Logs",
            font=("Segoe UI", 9), bg=CARD, fg=WHITE, relief="flat",
            cursor="hand2", pady=7,
            command=self._open_logs,
        ).pack(side="left", fill="x", expand=True, padx=(2, 2))

        tk.Button(
            row2, text="Open WebUI Admin",
            font=("Segoe UI", 9), bg=CARD, fg=DIM, relief="flat",
            cursor="hand2", pady=7,
            command=lambda: webbrowser.open(WEBUI + "/admin/models"),
        ).pack(side="left", fill="x", expand=True, padx=(2, 0))

        # Quick-open buttons — tunnel row (Cloudflare quick tunnel → Dexter :8080)
        trow = tk.Frame(btns, bg=BG)
        trow.pack(fill="x", pady=(0, 4))

        tk.Button(
            trow, text="🌐 Share Link",
            font=("Segoe UI", 9), bg=CARD, fg=GOLD, relief="flat",
            cursor="hand2", pady=7,
            command=self._start_tunnel,
        ).pack(side="left", fill="x", expand=True, padx=(0, 2))

        self._tunnel_lbl = tk.Label(
            trow, text="off", font=("Segoe UI", 8), bg=CARD, fg=DIM,
            anchor="w", cursor="hand2", pady=7,
        )
        self._tunnel_lbl.pack(side="left", fill="x", expand=True, padx=(2, 0))
        self._tunnel_lbl.bind("<Button-1>", lambda e: self._open_tunnel_link())

        # ── VS Code Terminal Checklist ───────────────────────────────────────
        chk_outer = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                             highlightthickness=1)
        chk_outer.pack(fill="x", padx=28, pady=(14, 0))

        hdr = tk.Frame(chk_outer, bg=CARD)
        hdr.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(hdr, text="STARTUP SCRIPTS",
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
                  text="Launcher starts these (guarded). Or copy the command to run manually in VS Code.",
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

    def _ollama_models(self):
        try:
            r = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return r.stdout
        except Exception:
            return ""

    def _build_brain(self):
        if self._check_brain():
            self._bar.config(
                text=f"Chev Brain ({CHEV_BRAIN_NAME} + {CHEV_BRAIN_LEARN_NAME}) "
                     f"already built — ready to use."
            )
            return

        self._bar.config(text=f"Building brains (one-time, ~30s each)…")

        def _do_build():
            try:
                existing = self._ollama_models()
                built = []
                for name, modelfile in (
                    (CHEV_BRAIN_NAME, CHEV_BRAIN_MODELFILE),
                    (CHEV_BRAIN_LEARN_NAME, CHEV_BRAIN_LEARN_MODELFILE),
                ):
                    if name in existing:
                        continue
                    subprocess.run(
                        ["ollama", "create", name, "-f", modelfile],
                        capture_output=True, text=True, timeout=300,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    existing = self._ollama_models()
                    built.append(name)

                if self._check_brain():
                    self._brain_built = True
                    self._status["brain"] = True
                    self.root.after(0, lambda: self._set_dot("brain", True))
                    self.root.after(
                        0,
                        lambda: self._bar.config(
                            text=f"{CHEV_BRAIN_NAME} + {CHEV_BRAIN_LEARN_NAME} built. Point the "
                                  f"clone at {CHEV_BRAIN_NAME} and learning at {CHEV_BRAIN_LEARN_NAME} "
                                  f"(Admin → Models), then restart Ollama."
                        ),
                    )
                else:
                    self.root.after(
                        0,
                        lambda: self._bar.config(
                            text="Build incomplete — one brain missing. Click Build Chev Brain again."
                        ),
                    )
            except Exception as e:
                self.root.after(0, lambda: self._bar.config(text=f"Brain build failed: {e}"))

        threading.Thread(target=_do_build, daemon=True).start()

    # ── Auto-start (guarded, hidden, logged) ─────────────────────────────────

    def _ensure_log_dir(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except Exception:
            pass

    def _log_path(self, name):
        return os.path.join(LOG_DIR, name + ".log")

    def _py_running(self, script):
        try:
            r = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return script in r.stdout
        except Exception:
            return False

    def _ollama_up(self):
        return _check("http://localhost:11434", timeout=4)

    def _start_ollama(self):
        if self._ollama_up():
            self._bar.config(text="Ollama already running.")
            return
        try:
            self._ensure_log_dir()
            f = open(self._log_path("ollama"), "ab", 0)
            subprocess.Popen(["ollama", "serve"], stdout=f, stderr=f,
                             creationflags=subprocess.CREATE_NO_WINDOW)
            self._bar.config(text="Ollama starting…")
        except Exception as e:
            self._bar.config(text=f"Ollama start failed: {e}")

    def _start_dexter(self):
        if self._py_running("dexter"):
            self._bar.config(text="Dexter already running.")
            return
        try:
            self._ensure_log_dir()
            f = open(self._log_path("dexter"), "ab", 0)
            p = subprocess.Popen([sys.executable, DEXTER_SCRIPT], stdout=f, stderr=f,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            self._procs["dexter"] = p.pid
            self._bar.config(text="Dexter starting…")
        except Exception as e:
            self._bar.config(text=f"Dexter start failed: {e}")

    def _start_telegram(self):
        if self._check_telegram():
            self._bar.config(text="Telegram already running.")
            return
        try:
            self._ensure_log_dir()
            f = open(self._log_path("telegram"), "ab", 0)
            p = subprocess.Popen([sys.executable, TELEGRAM_SCRIPT], stdout=f, stderr=f,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            self._procs["telegram"] = p.pid
            self._bar.config(text="Telegram starting…")
        except Exception as e:
            self._bar.config(text=f"Telegram start failed: {e}")

    def _start_tunnel(self):
        if self._tunnel_proc is not None:
            self._bar.config(text="Tunnel already running — link is in the Share Link box.")
            return
        # Dexter must be serving :8080 for the tunnel to have something to point at
        if not _check(TERMINAL, timeout=2):
            self._bar.config(text="Dexter not up on :8080 — starting it…")
            self._start_dexter()

        self._tunnel_lbl.config(text="starting…", fg=DIM)
        try:
            self._ensure_log_dir()
            f = open(self._log_path("tunnel"), "ab", 0)
            proc = subprocess.Popen(
                [CLOUDFLARED_EXE, "tunnel", "--url", TERMINAL],
                stdout=f, stderr=f,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._tunnel_proc = proc
        except Exception as e:
            self._tunnel_lbl.config(text="failed", fg=RED)
            self._bar.config(text=f"Tunnel start failed: {e}")
            return

        def _watch():
            import re as _re
            url_re = _re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
            for _ in range(60):  # up to ~30s for the URL to appear
                try:
                    with open(self._log_path("tunnel"), "r",
                              encoding="utf-8", errors="replace") as fh:
                        seen = fh.read()
                except Exception:
                    seen = ""
                m = url_re.search(seen)
                if m:
                    self._set_tunnel_live(m.group(0))
                    return
                time.sleep(0.5)
            self.root.after(
                0, lambda: self._tunnel_lbl.config(text="no url — see logs/tunnel", fg=RED)
            )

        threading.Thread(target=_watch, daemon=True).start()

    def _set_tunnel_live(self, url):
        self._tunnel_url = url
        self._tunnel_lbl.config(text=url, fg=GREEN)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
        except Exception:
            pass
        self._bar.config(text=f"Tunnel live — link copied to clipboard: {url}")

    def _open_tunnel_link(self):
        if self._tunnel_url:
            webbrowser.open(self._tunnel_url)
        else:
            self._bar.config(text="No tunnel link yet — click 🌐 Share Link first.")

    def _open_logs(self):
        # Single instance: focus the existing viewer instead of stacking threads
        viewer = getattr(self, "_logs_viewer", None)
        if viewer is not None and viewer.winfo_exists():
            viewer.lift()
            return

        viewer = tk.Toplevel(self.root)
        self._logs_viewer = viewer
        viewer.title("Chev Logs")
        viewer.geometry("720x520")
        viewer.configure(bg=BG)

        txt = tk.Text(viewer, bg="#0d1117", fg=WHITE,
                      font=("Courier New", 9), wrap="none", state="disabled")
        txt.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        scroll = tk.Scrollbar(viewer, command=txt.yview)
        txt.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        stop = threading.Event()

        def _on_close():
            stop.set()
            viewer.destroy()
        viewer.protocol("WM_DELETE_WINDOW", _on_close)

        def _render(out):
            try:
                txt.config(state="normal")
                txt.delete("1.0", "end")
                txt.insert("end", out)
                txt.see("end")
                txt.config(state="disabled")
            except Exception:
                pass  # widget already destroyed

        def _tail():
            while not stop.is_set():
                try:
                    parts = []
                    for n in ("ollama", "dexter", "telegram"):
                        p = self._log_path(n)
                        if os.path.exists(p):
                            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                                lines = fh.read().splitlines()
                            parts.append(f"===== {n} (last 250 lines) =====")
                            parts.extend(lines[-250:])
                    if not stop.is_set():
                        txt.after(0, lambda o="\n".join(parts): _render(o))
                except Exception:
                    pass
                time.sleep(1.5)

        threading.Thread(target=_tail, daemon=True).start()

    # ── Logic ────────────────────────────────────────────────────────────────

    def _launch_everything(self):
        if not self._status["docker"]:
            self._start_docker()
            return

        # Start the brain + bots (each guarded so double-clicks never double-start)
        self._start_ollama()
        self._start_dexter()
        self._start_telegram()

        try:
            subprocess.Popen(
                [CHROME_EXE, "--new-window", MONITOR_URL, WEBUI],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            webbrowser.open(MONITOR_URL)
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
            brain    = self._check_brain()

            self._status = {
                "docker": docker, "webui": webui, "dexter": dexter,
                "gemini": gemini, "telegram": telegram, "brain": brain,
            }
            self.root.after(0, self._apply_status)
            time.sleep(5)

    def _apply_status(self):
        for key in ("docker", "webui", "dexter", "gemini", "telegram", "brain"):
            self._set_dot(key, self._status[key])

        all_up = all(self._status.values())
        if all_up:
            self._launch_btn.config(bg="#0d3320", fg=GREEN)
        else:
            self._launch_btn.config(bg="#1e3a5f", fg=WHITE)

        self._bar.config(text=f"Last checked: {time.strftime('%H:%M:%S')}")

    def _check_brain(self):
        out = self._ollama_models()
        return (
            CHEV_BRAIN_NAME in out
            and CHEV_BRAIN_LEARN_NAME in out
        )

    def _set_dot(self, key, online):
        self._dots[key].config(fg=GREEN if online else RED)


if __name__ == "__main__":
    ChevLauncher()
