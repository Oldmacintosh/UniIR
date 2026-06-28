# -*- coding: utf-8 -*-
"""
demo.py  —  demo GUI for the `UniIr` library.

A tkinter front-end that uses the UniIr class to capture and replay IR commands,
manage a command library, and configure the ESP32's activity automation. All the
device logic (self-healing connection, protocol, capture/replay, provisioning,
the automation runner) lives in __init__.py — this file is just UI wiring, and a
worked example of how to drive the library.

"""

import argparse
import atexit
import queue
import threading
import time

import tkinter as tk
from tkinter import ttk, simpledialog, messagebox

try:  # as a package:  python -m UniIr.demo
    from . import (UniIr, Automation, load_data, save_data,
                   describe, to_spec, default_auto, DEFAULT_PORT, MAX_AUTO)
except ImportError:  # run directly in the folder:  python demo.py
    from __init__ import (UniIr, Automation, load_data, save_data,
                          describe, to_spec, default_auto, DEFAULT_PORT, MAX_AUTO)


class App(tk.Tk):
    def __init__(self, port):
        super().__init__()
        self.ui_q = queue.Queue()
        self.data = load_data()
        self.lib = self.data["commands"]
        self.cfg = self.data["automation"]
        self.auto_on = bool(
            self.cfg.get("enabled", True))  # automation intent (persisted; first boot = on)

        self.esp = UniIr(port, on_status=self._on_conn, on_log=self.log)
        atexit.register(self.esp.close)
        self.auto = Automation(self.esp, self.data, self.log, self.set_state)
        self.auto.debug = bool(self.cfg.get("debug", False))
        self.debug_var = tk.BooleanVar(value=self.auto.debug)

        self.title("ESP32 Universal Remote")
        self.geometry("900x600")
        self._build()
        self.refresh_all()
        self._refresh_run_btn()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ----
    def _build(self):
        bar = ttk.Frame(self, padding=(8, 4)); bar.pack(fill="x")
        self.conn_lbl = tk.Label(bar, text="● Disconnected — retrying every 5 s", fg="#c0392b")
        self.conn_lbl.pack(side="left")

        top = ttk.Frame(self, padding=8); top.pack(fill="both", expand=True)

        left = ttk.LabelFrame(top, text="Commands", padding=6)
        left.pack(side="left", fill="both", expand=True)
        self.cmd_list = tk.Listbox(left, height=16, exportselection=False)
        self.cmd_list.pack(fill="both", expand=True)
        b = ttk.Frame(left); b.pack(fill="x", pady=4)
        ttk.Button(b, text="Capture", command=self.on_capture).pack(side="left")
        ttk.Button(b, text="Send", command=self.on_send).pack(side="left")
        ttk.Button(b, text="Rename", command=self.on_rename).pack(side="left")
        ttk.Button(b, text="Delete", command=self.on_delete).pack(side="left")
        a = ttk.Frame(left); a.pack(fill="x")
        ttk.Button(a, text="→ Add to activity", command=lambda: self.add_to("activity")).pack(side="left")
        ttk.Button(a, text="→ Add to no-activity", command=lambda: self.add_to("idle")).pack(side="left")

        right = ttk.Frame(top); right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        af = ttk.LabelFrame(right, text="On activity (turn ON)", padding=6)
        af.pack(fill="both", expand=True)
        self.act_list = tk.Listbox(af, height=6, exportselection=False)
        self.act_list.pack(fill="both", expand=True)
        ab = ttk.Frame(af); ab.pack(fill="x", pady=2)
        ttk.Button(ab, text="Toggle standalone", command=lambda: self.toggle_auto("activity")).pack(side="left")
        ttk.Button(ab, text="Remove", command=lambda: self.remove_from("activity")).pack(side="left")

        idf = ttk.LabelFrame(right, text="On no activity (turn OFF)", padding=6)
        idf.pack(fill="both", expand=True, pady=(6, 0))
        self.idle_list = tk.Listbox(idf, height=6, exportselection=False)
        self.idle_list.pack(fill="both", expand=True)
        ib = ttk.Frame(idf); ib.pack(fill="x", pady=2)
        ttk.Button(ib, text="Toggle standalone", command=lambda: self.toggle_auto("idle")).pack(side="left")
        ttk.Button(ib, text="Remove", command=lambda: self.remove_from("idle")).pack(side="left")

        sf = ttk.Frame(right); sf.pack(fill="x", pady=(6, 0))
        ttk.Label(sf, text="Idle timeout (min):").pack(side="left")
        self.timeout_var = tk.StringVar(value=str(self.cfg["timeout_sec"] // 60))
        ttk.Entry(sf, width=5, textvariable=self.timeout_var).pack(side="left")
        ttk.Label(sf, text="Mic threshold:").pack(side="left", padx=(8, 0))
        self.thr_var = tk.StringVar(value=str(self.cfg["mic_threshold"]))
        ttk.Entry(sf, width=7, textvariable=self.thr_var).pack(side="left")
        ttk.Button(sf, text="Calibrate", command=self.on_calibrate).pack(side="left", padx=4)
        ttk.Button(sf, text="Save", command=self.save_settings).pack(side="left")

        cf = ttk.Frame(right); cf.pack(fill="x", pady=(6, 0))
        ttk.Button(cf, text="Push standalone to ESP32", command=self.on_provision).pack(side="left")
        ttk.Button(cf, text="Clear ESP32", command=self.on_clear_esp32).pack(side="left", padx=4)
        self.run_btn = ttk.Button(cf, text="Start automation", command=self.on_toggle_run)
        self.run_btn.pack(side="left", padx=6)
        self.state_lbl = ttk.Label(cf, text="OFF")
        self.state_lbl.pack(side="left")
        ttk.Checkbutton(cf, text="Debug log", variable=self.debug_var,
                        command=self._toggle_debug).pack(side="left", padx=(10, 0))

        logf = ttk.LabelFrame(self, text="Log", padding=4); logf.pack(fill="both", padx=8, pady=(0, 8))
        self.log_txt = tk.Text(logf, height=8, state="disabled", wrap="word")
        self.log_txt.pack(fill="both", expand=True)

    # ---- thread-safe UI plumbing ----
    def _pump(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def post(self, fn):
        self.ui_q.put(fn)

    def log(self, msg):
        self.post(lambda: self._append(msg))

    def _append(self, msg):
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
        self.log_txt.see("end")
        self.log_txt.config(state="disabled")

    def set_state(self, s):
        self.post(lambda: self.state_lbl.config(text=s))

    # ---- connection status ----
    def _on_conn(self, connected):
        self.post(lambda: self._update_conn(connected))

    def _update_conn(self, connected):
        if connected:
            self.conn_lbl.config(text="● Connected", fg="#2e7d32")
            self._append("Connected to ESP32.")
            self._apply_automation()     # sync stored intent (first boot = ON) + AUTOEN
        else:
            self.conn_lbl.config(text="● Disconnected — retrying every 5 s", fg="#c0392b")
            self._append("Disconnected. Retrying every 5 s…")
            if self.auto.running:
                self.auto.stop()
                self._append("Automation stopped — it needs the device.")
            self._refresh_run_btn()

    # ---- helpers ----
    def _require_conn(self):
        if not self.esp.connected:
            self.log("Not connected — waiting for the ESP32.")
            return False
        return True

    def selected_cmd(self):
        s = self.cmd_list.curselection()
        return sorted(self.lib)[s[0]] if s else None

    def refresh_all(self):
        self.cmd_list.delete(0, "end")
        for n in sorted(self.lib):
            self.cmd_list.insert("end", f"{n}  [{describe(self.lib[n])}]")
        self._refresh_auto(self.act_list, "activity")
        self._refresh_auto(self.idle_list, "idle")

    def _refresh_auto(self, widget, key):
        widget.delete(0, "end")
        for e in self.cfg[key]:
            widget.insert("end", e["name"] + (" [standalone]" if e.get("autonomous") else ""))

    # ---- command actions ----
    def on_capture(self):
        if not self._require_conn():
            return
        name = simpledialog.askstring("Capture", "Name for this command:", parent=self)
        if not name:
            return
        if name in self.lib and not messagebox.askyesno("Overwrite", f"'{name}' exists. Overwrite?"):
            return
        self.log("Capturing… press the remote button.")

        def work():
            cmd = self.esp.capture()
            if not cmd:
                self.log("No signal captured (timed out / offline).")
                return
            self.lib[name] = cmd
            save_data(self.data)
            self.log(f"Saved '{name}'  [{describe(cmd)}]")
            self.post(self.refresh_all)

        threading.Thread(target=work, daemon=True).start()

    def on_send(self):
        n = self.selected_cmd()
        if not n or not self._require_conn():
            return
        self.log(f"Sending '{n}'…")
        threading.Thread(target=lambda: self.log("Sent." if self.esp.send(self.lib[n]) else "Send failed."),
            daemon=True).start()

    def on_rename(self):
        n = self.selected_cmd()
        if not n:
            return
        new = simpledialog.askstring("Rename", "New name:", initialvalue=n, parent=self)
        if new and new != n:
            self.lib[new] = self.lib.pop(n)
            for key in ("activity", "idle"):
                for e in self.cfg[key]:
                    if e["name"] == n:
                        e["name"] = new
            save_data(self.data); self.refresh_all()

    def on_delete(self):
        n = self.selected_cmd()
        if n and messagebox.askyesno("Delete", f"Delete '{n}'?"):
            del self.lib[n]
            for key in ("activity", "idle"):
                self.cfg[key] = [e for e in self.cfg[key] if e["name"] != n]
            save_data(self.data); self.refresh_all()

    # ---- automation config ----
    def add_to(self, key):
        n = self.selected_cmd()
        if not n or any(e["name"] == n for e in self.cfg[key]):
            return
        self.cfg[key].append({"name": n, "autonomous": False})
        save_data(self.data); self.refresh_all()

    def remove_from(self, key):
        w = self.act_list if key == "activity" else self.idle_list
        s = w.curselection()
        if s:
            self.cfg[key].pop(s[0]); save_data(self.data); self.refresh_all()

    def toggle_auto(self, key):
        w = self.act_list if key == "activity" else self.idle_list
        s = w.curselection()
        if not s:
            return
        e = self.cfg[key][s[0]]
        if not e.get("autonomous") and self.lib[e["name"]]["type"] == "raw":
            messagebox.showinfo("Not allowed", "Raw commands can't run standalone on the ESP32.")
            return
        e["autonomous"] = not e.get("autonomous")
        save_data(self.data); self.refresh_all()

    def save_settings(self):
        try:
            self.cfg["timeout_sec"] = max(60, int(self.timeout_var.get()) * 60)
            self.cfg["mic_threshold"] = float(self.thr_var.get())
            save_data(self.data); self.log("Settings saved.")
        except ValueError:
            messagebox.showerror("Bad value", "Timeout must be whole minutes, threshold a decimal.")

    def on_calibrate(self):
        self.log("Calibrating mic for 3s — stay quiet…")

        def work():
            try:
                import sounddevice as sd, numpy as np
            except ImportError:
                self.log("Needs: pip install sounddevice numpy"); return
            rec = sd.rec(int(3 * 16000), samplerate=16000, channels=1, dtype="float32"); sd.wait()
            amb = float(np.sqrt(np.mean(rec ** 2)))
            thr = round(max(0.02, amb * 4), 4)
            self.cfg["mic_threshold"] = thr
            save_data(self.data)
            self.post(lambda: self.thr_var.set(str(thr)))
            self.log(f"Ambient {amb:.4f} -> threshold {thr}")

        threading.Thread(target=work, daemon=True).start()

    def on_provision(self):
        if not self._require_conn():
            return
        act = [e for e in self.cfg["activity"] if e.get("autonomous")][:MAX_AUTO]
        idle = [e for e in self.cfg["idle"] if e.get("autonomous")][:MAX_AUTO]
        if not act and not idle:
            messagebox.showinfo("Nothing to push", "Mark some commands as [standalone] first.")
            return
        try:
            act_specs = [to_spec(self.lib[e["name"]]) for e in act]
            idle_specs = [to_spec(self.lib[e["name"]]) for e in idle]
        except (KeyError, ValueError) as ex:
            messagebox.showerror("Cannot push", str(ex)); return
        self.log("Pushing standalone config to ESP32…")

        def work():
            ok, where = self.esp.provision(act_specs, idle_specs, self.cfg["timeout_sec"])
            self.log(f"Pushed {len(act_specs)} activity + {len(idle_specs)} idle command(s)."
                     if ok else f"Push failed at {where}.")

        threading.Thread(target=work, daemon=True).start()

    def on_clear_esp32(self):
        if not self._require_conn():
            return
        if not messagebox.askyesno("Clear ESP32", "Erase all standalone commands stored on the ESP32?"):
            return
        threading.Thread(target=lambda: self.log(
            "ESP32 standalone commands cleared." if self.esp.clear_standalone() else "Clear failed."),
                         daemon=True).start()

    # ---- automation start/stop (+ ESP32 standalone enable) ----
    def set_standalone(self, enabled):
        def work():
            ok = self.esp.set_standalone(enabled)
            self.log(("Standalone enabled on ESP32." if enabled else "Standalone disabled on ESP32.")
                if ok else "Couldn't reach ESP32 to change standalone (will apply when reconnected).")

        threading.Thread(target=work, daemon=True).start()

    def _toggle_debug(self):
        self.auto.debug = self.debug_var.get()
        self.cfg["debug"] = self.auto.debug
        save_data(self.data)
        self.log(f"Automation debug logging {'ON' if self.auto.debug else 'OFF'}.")

    def _refresh_run_btn(self):
        running = self.auto.running
        self.run_btn.config(text="Stop automation" if running else "Start automation",
                            state=("normal" if self.esp.connected else "disabled"))

    def _apply_automation(self):
        # Bring the PC runner + ESP32 standalone flag in line with the stored
        # intent (self.auto_on). Requires a live connection.
        if not self.esp.connected:
            if self.auto.running:
                self.auto.stop()
            self._refresh_run_btn()
            return
        if self.auto_on:
            self.set_standalone(True)
            if not self.auto.running and not self.auto.start():
                self.set_state("standalone only")
        else:
            if self.auto.running:
                self.auto.stop()
            self.set_standalone(False)
        self._refresh_run_btn()

    def _set_automation(self, on):
        self.auto_on = on
        self.cfg["enabled"] = on  # remember the choice across reconnects / restarts
        save_data(self.data)
        self._apply_automation()

    def on_toggle_run(self):
        if not self.esp.connected:
            self.log("Connect to the ESP32 first — automation needs the device.")
            return
        turning_on = not self.auto.running
        if turning_on:
            self.save_settings()  # capture any unsaved timeout / threshold edits
        self._set_automation(turning_on)

    def _on_close(self):
        try:
            if self.auto.running:
                self.auto.stop()
        finally:
            self.esp.close()
            self.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=DEFAULT_PORT)
    args = ap.parse_args()
    App(args.port).mainloop()


if __name__ == "__main__":
    main()