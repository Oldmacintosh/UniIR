# -*- coding: utf-8 -*-
"""
UniIr  —  a small Python library to control the ESP32 Universal Remote.

It talks to the ESP32 over its Bluetooth/USB serial link and handles the messy
parts for you: a self-healing connection (auto-reconnect every few seconds, a
handshake so it never reports a dead link as "connected", and a liveness
watchdog), the line protocol, capturing and replaying IR commands (normal
remotes, stateful AC units, and raw), and provisioning the ESP32's standalone
activity automation.

Quick start
-----------
    from UniIr import UniIr

    remote = UniIr("/dev/cu.uniir",
                on_status=lambda up: print("connected" if up else "disconnected"),
                on_log=print)

    # capture a button (press the remote when prompted by your own UI)
    cmd = remote.capture()              # -> dict, or None on timeout
    # ... store cmd somewhere ...
    remote.send(cmd)                    # replay it (blocks until the ESP32 confirms)

    # stream motion events
    remote.pir_handler = lambda line: print("motion!" if line == "PIR 1" else "still")

    remote.close()

The optional `Automation` class fuses keyboard/mouse/mic/PIR activity on the PC
to drive the remote, and `load_data`/`save_data` persist a command library to JSON.
"""

import json
import os
import queue
import threading
import time

try:
    import serial
except ImportError as e:  # pragma: no cover
    raise ImportError("UniIr requires pyserial:  pip install pyserial") from e

__all__ = [
    "UniIr", "Automation",
    "describe", "to_spec", "is_storable",
    "load_data", "save_data", "default_auto",
    "DEFAULT_PORT", "BAUD", "MAX_AUTO", "LIBRARY_FILE",
]

# ---- defaults / tunables ----
DEFAULT_PORT = "COM3"  # macOS; use "COM5" on Windows
BAUD = 115200
LIBRARY_FILE = "commands.json"
CHUNK = 16  # raw values per LOADRAW line
HEARTBEAT_SEC = 5  # heartbeat interval
RECONNECT_SEC = 5  # retry interval while offline
LIVENESS_TIMEOUT = 12  # no reply this long => link is dead
MAX_AUTO = 4  # max standalone cmds per list on the ESP32


# ===================== command model =====================
# A "command" is a plain dict, one of:
#   {"type":"code",  "proto":int, "name":str, "bits":int, "value":hexstr}
#   {"type":"state", "proto":int, "name":str, "nbytes":int, "bytes":[hexstr,...]}
#   {"type":"raw",   "name":"RAW", "data":[int,...]}

def describe(cmd):
    """Human-readable one-liner for a command."""
    if cmd["type"] == "code":
        return f"{cmd['name']} {cmd['bits']}-bit 0x{cmd['value'].upper()}"
    if cmd["type"] == "state":
        return f"{cmd['name']} (AC) {cmd['nbytes']}B"
    return f"RAW ({len(cmd['data'])} edges)"


def is_storable(cmd):
    """True if the command can be stored/run standalone on the ESP32 (not raw)."""
    return cmd["type"] in ("code", "state")


def to_spec(cmd):
    """Encode a command as an AUTOACT/AUTOIDLE spec string. Raises for raw."""
    if cmd["type"] == "code":
        return f"CODE {cmd['proto']} {cmd['bits']} {cmd['value']}"
    if cmd["type"] == "state":
        return f"STATE {cmd['proto']} {cmd['nbytes']} " + " ".join(cmd["bytes"])
    raise ValueError("raw commands can't be stored on the ESP32")


# ===================== JSON storage (optional helper) =====================
def default_auto():
    return {"timeout_sec": 600, "mic_threshold": 0.05,
            "activity": [], "idle": [], "enabled": True, "debug": False}


def load_data(path=LIBRARY_FILE):
    """Load a {"commands":{...}, "automation":{...}} library from JSON, migrating
    the old flat format and filling in any missing automation defaults."""
    if os.path.exists(path):
        try:
            with open(path) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"commands": {}, "automation": default_auto()}
        if "commands" not in d:
            d = {"commands": d, "automation": default_auto()}
        d.setdefault("automation", default_auto())
        for k, v in default_auto().items():
            d["automation"].setdefault(k, v)
        return d
    return {"commands": {}, "automation": default_auto()}


def save_data(data, path=LIBRARY_FILE):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class UniIr:
    """Self-healing client for the ESP32 Universal Remote.

    Opens and maintains the serial link on background threads: it auto-connects,
    verifies the link with a PING/PONG handshake, watches a heartbeat to detect
    silent drops (common on macOS Bluetooth), and reconnects every RECONNECT_SEC.

    Parameters
    ----------
    port : str            serial device, e.g. "/dev/cu.uniir" or "COM5"
    baud : int            baud rate (default 115200)
    on_status(bool)       called whenever the connected state changes
    on_log(str)           called with human-readable status/diagnostic messages
    auto_connect : bool   start the connection threads immediately (default True)

    Attributes
    ----------
    connected : bool          True only after a verified handshake
    pir_handler : callable    set to a function(line:str) to receive async
                              "PIR 1"/"PIR 0" motion lines from the ESP32
    """

    def __init__(self, port=DEFAULT_PORT, baud=BAUD,
                 on_status=None, on_log=None, auto_connect=True):
        self.port = port
        self.baud = baud
        self.on_status = on_status or (lambda c: None)
        self.on_log = on_log or (lambda m: None)
        self.ser = None
        self.connected = False
        self.alive = True
        self.last_rx = time.time()
        self.wlock = threading.Lock()
        self.txn = threading.Lock()
        self.q = queue.Queue()
        self.pir_handler = None
        if auto_connect:
            self.start()

    def start(self):
        """Start the background connection/reader/heartbeat threads."""
        threading.Thread(target=self._manager, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._hb_loop, daemon=True).start()

    # ---- connection lifecycle ----
    def _set_connected(self, val):
        if val != self.connected:
            self.connected = val
            try:
                self.on_status(val)
            except Exception:
                pass

    def _open(self):
        try:
            s = serial.Serial(self.port, self.baud, timeout=0.2)
        except (serial.SerialException, OSError, ValueError):
            return False
        time.sleep(2.0)
        try:
            s.reset_input_buffer()
        except Exception:
            pass
        self.ser = s  # reader starts reading now
        self.last_rx = time.time()
        # Handshake. macOS can "open" a stale Bluetooth port that carries no
        # data, so only declare connected once the ESP32 actually answers.
        self._drain()
        self._write("PING")
        if self._wait("PONG", 3.0):
            self._set_connected(True)
            return True
        self.on_log("Port opened but ESP32 didn't answer — retrying. If this keeps "
                    "happening on macOS, remove the device in Bluetooth settings and pair again.")
        self.ser = None
        try:
            s.close()
        except Exception:
            pass
        return False

    def _drop(self):
        self._set_connected(False)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def _manager(self):
        while self.alive:
            if not self.connected:
                if not self._open():
                    for _ in range(int(RECONNECT_SEC * 10)):  # ~5s, stays responsive
                        if not self.alive:
                            return
                        time.sleep(0.1)
                    continue
            else:
                time.sleep(0.5)

    def _reader(self):
        buf = bytearray()
        while self.alive:
            s = self.ser
            if s is None:
                buf = bytearray()
                time.sleep(0.2)
                continue
            try:
                b = s.read(1)
            except (serial.SerialException, OSError, TypeError):
                self._drop(); buf = bytearray(); continue
            if not b:
                continue
            if b == b"\n":
                line = buf.decode(errors="ignore").strip()
                buf = bytearray()
                if not line:
                    continue
                self.last_rx = time.time()  # any line means the link is alive
                if line == "HBOK":
                    continue  # heartbeat reply, consume it
                if line.startswith("PIR "):
                    if self.pir_handler:
                        self.pir_handler(line)
                else:
                    self.q.put(line)
            elif b != b"\r":
                buf += b

    def _hb_loop(self):
        while self.alive:
            time.sleep(HEARTBEAT_SEC)
            if not self.connected:
                continue
            self._write("HB")
            # Liveness watchdog: macOS often won't error on a stale Bluetooth
            # port after the ESP32 resets, so detect death by missing replies
            # and force a reconnect. Skip while a request holds the link
            # (capture can legitimately keep the ESP32 busy for ~20s).
            if not self.txn.locked() and time.time() - self.last_rx > LIVENESS_TIMEOUT:
                self._drop()

    # ---- low-level io ----
    def _write(self, line):
        s = self.ser
        if s is None:
            return False
        with self.wlock:
            try:
                s.write((line + "\n").encode())
                return True
            except (serial.SerialException, OSError):
                self._drop()
                return False

    def _drain(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def _wait(self, prefix, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                l = self.q.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                return None
            if l.startswith(prefix):
                return l
        return None

    # ---- public request/response operations ----
    def ping(self, timeout=3):
        """Return True if the ESP32 answers a PING."""
        if not self.connected:
            return False
        with self.txn:
            self._drain()
            return self._write("PING") and self._wait("PONG", timeout) is not None

    def capture(self, timeout=25):
        """Ask the ESP32 to capture the next IR signal. Returns a command dict
        (code/state/raw), or None on timeout / no signal."""
        if not self.connected:
            return None
        with self.txn:
            self._drain()
            if not self._write("CAP"):
                return None
            end = time.time() + timeout
            while time.time() < end:
                try:
                    l = self.q.get(timeout=max(0.05, end - time.time()))
                except queue.Empty:
                    break
                p = l.split()
                if l.startswith("CODE"):
                    return {"type": "code", "proto": int(p[1]), "name": p[2],
                            "bits": int(p[3]), "value": p[4]}
                if l.startswith("STATE"):
                    nb = int(p[3])
                    return {"type": "state", "proto": int(p[1]), "name": p[2],
                            "nbytes": nb, "bytes": p[4:4 + nb]}
                if l.startswith("RAW"):
                    n = int(p[1])
                    return {"type": "raw", "name": "RAW", "data": [int(x) for x in p[2:2 + n]]}
                if l.startswith("TIMEOUT"):
                    return None
            return None

    def send(self, cmd):
        """Replay a command and block until the ESP32 confirms (returns bool)."""
        if not self.connected:
            return False
        with self.txn:
            self._drain()
            if cmd["type"] == "code":
                return self._write(f"SEND CODE {cmd['proto']} {cmd['bits']} {cmd['value']}") \
                    and self._wait("SENT", 4) is not None
            if cmd["type"] == "state":
                return self._write(f"SEND STATE {cmd['proto']} {cmd['nbytes']} " + " ".join(cmd["bytes"])) \
                    and self._wait("SENT", 4) is not None
            vals = cmd["data"]
            if not self._write(f"LOADRAW {len(vals)}") or not self._wait("ACK", 3):
                return False
            for i in range(0, len(vals), CHUNK):
                if not self._write("D " + " ".join(str(v) for v in vals[i:i + CHUNK])) or not self._wait("ACK", 3):
                    return False
            return self._write("FIRE") and self._wait("SENT", 4) is not None

    def send_async(self, cmd):
        """Fire a code/state command without waiting for confirmation. Used by
        automation where round-trip latency would slow the loop. Raw is ignored."""
        if cmd["type"] == "code":
            self._write(f"SEND CODE {cmd['proto']} {cmd['bits']} {cmd['value']}")
        elif cmd["type"] == "state":
            self._write(f"SEND STATE {cmd['proto']} {cmd['nbytes']} " + " ".join(cmd["bytes"]))

    def provision(self, activity_specs, idle_specs, timeout_sec):
        """Store the standalone automation on the ESP32. Specs are strings from
        to_spec(). Returns (ok: bool, where: str) where `where` names the step
        that failed (or the last step on success)."""
        if not self.connected:
            return False, "offline"
        with self.txn:
            self._drain()
            if not self._write(f"AUTOCFG {timeout_sec}") or not self._wait("ACK", 3):
                return False, "AUTOCFG"
            for s in activity_specs:
                if not self._write("AUTOACT " + s) or not self._wait("ACK", 3):
                    return False, "AUTOACT"
            for s in idle_specs:
                if not self._write("AUTOIDLE " + s) or not self._wait("ACK", 3):
                    return False, "AUTOIDLE"
            return (self._write("AUTOSAVE") and self._wait("ACK", 3) is not None), "AUTOSAVE"

    def set_standalone(self, enabled):
        """Enable/disable the ESP32 running its stored automation on its own."""
        if not self.connected:
            return False
        with self.txn:
            self._drain()
            return self._write(f"AUTOEN {1 if enabled else 0}") and self._wait("ACK", 3) is not None

    def clear_standalone(self):
        """Erase the standalone commands stored on the ESP32."""
        if not self.connected:
            return False
        with self.txn:
            self._drain()
            return self._write("AUTOCLEAR") and self._wait("ACK", 3) is not None

    def close(self):
        """Stop the threads and release the serial port cleanly."""
        self.alive = False
        time.sleep(0.2)
        if self.ser:
            try:
                self.ser.flush()
            except Exception:
                pass
            try:
                self.ser.close()
            except Exception:
                pass


class Automation:
    """Fuses keyboard, mouse, microphone loudness and PIR motion into a single
    "active / idle" signal and drives the remote: it fires the `activity` commands
    when activity resumes and the `idle` commands after `timeout_sec` of quiet.

    Parameters
    ----------
    remote : UniIr
    data : dict            the {"commands":..., "automation":...} library; reads
                           data["automation"]["activity"|"idle"|"timeout_sec"|
                           "mic_threshold"] and data["commands"]
    log(str)               status logger
    on_state(str)          called with "ACTIVE" / "IDLE" / "OFF"

    Set `.debug = True` for verbose logging of what resets the trigger.
    Needs pynput, sounddevice and numpy installed to actually run.
    """

    def __init__(self, remote, data, log, on_state):
        self.esp = remote
        self.data = data
        self.log = log
        self.on_state = on_state
        self.running = False
        self.lock = threading.Lock()
        self.on = False
        self.last = time.time()
        self._listeners = []
        self._stream = None
        self.debug = False
        self.debug_interval = 1.0   # min seconds between debug logs per source
        self._dbg_last = {}

    def start(self):
        try:
            from pynput import keyboard, mouse
            import sounddevice as sd
            import numpy as np
        except ImportError:
            self.log("Install runner deps:  pip install pynput sounddevice numpy")
            return False

        cfg = self.data["automation"]
        if not cfg["activity"] and not cfg["idle"]:
            self.log("Add some commands to the activity/idle lists first.")
            return False

        self.timeout = cfg["timeout_sec"]
        self.threshold = cfg["mic_threshold"]
        self.running = True
        self.on = True  # assume devices are already on at start
        self.last = time.time()  # begin the idle countdown immediately

        kl = keyboard.Listener(on_press=lambda k: self._activity("keyboard"))
        ml = mouse.Listener(on_move=lambda *a: self._activity("mouse"),
                            on_click=lambda *a: self._activity("mouse"),
                            on_scroll=lambda *a: self._activity("mouse"))
        kl.start(); ml.start()
        self._listeners = [kl, ml]

        def mic_cb(indata, frames, t, status):
            if float(np.sqrt(np.mean(indata ** 2))) > self.threshold:
                self._activity("sound")

        self._stream = sd.InputStream(channels=1, samplerate=16000,
                                      blocksize=1600, callback=mic_cb)
        self._stream.start()

        self.esp.pir_handler = self._on_pir
        threading.Thread(target=self._idle_loop, daemon=True).start()

        self.log(f"Automation ON — assuming devices on, idle timeout {self.timeout // 60} min."
                 + ("   [debug logging ON]" if self.debug else ""))
        self.on_state("ACTIVE")
        return True

    def _on_pir(self, line):
        if line == "PIR 1":
            self._activity("motion")
        elif self.debug:
            self.log("[debug] PIR 0 — motion cleared")

    def stop(self):
        self.running = False
        self.esp.pir_handler = None
        for l in self._listeners:
            try:
                l.stop()
            except Exception:
                pass
        self._listeners = []
        if self._stream:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.log("Automation OFF.")
        self.on_state("OFF")

    def _fire(self, key):
        lib = self.data["commands"]
        for e in self.data["automation"][key]:
            cmd = lib.get(e["name"])
            if not cmd or cmd["type"] == "raw":
                continue
            self.esp.send_async(cmd)
            if self.debug:
                self.log(f"[debug] sent '{e['name']}'  ({key})")
            time.sleep(0.06)

    def _activity(self, source):
        now = time.time()
        with self.lock:
            self.last = now
            if not self.on:
                self.on = True
                self._fire("activity")
                self.log(f"activity ({source}) -> ON")
                self.on_state("ACTIVE")
            elif self.debug:
                # throttle per source so high-rate events (mouse/sound) don't flood the log
                if now - self._dbg_last.get(source, 0) >= self.debug_interval:
                    self._dbg_last[source] = now
                    self.log(f"[debug] timer reset by {source} — idle countdown back to {self.timeout // 60} min")

    def _idle_loop(self):
        last_print = 0
        while self.running:
            time.sleep(1)
            with self.lock:
                idle_for = time.time() - self.last
                if self.on and idle_for > self.timeout:
                    self.on = False
                    self._fire("idle")
                    self.log(f"idle {self.timeout // 60} min -> OFF")
                    self.on_state("IDLE")
                elif self.debug and self.on and time.time() - last_print >= 5:
                    last_print = time.time()
                    self.log(f"[debug] quiet for {int(idle_for)}s — off in {int(self.timeout - idle_for)}s")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="UniIr connectivity test")
    ap.add_argument("--port", default=DEFAULT_PORT)
    a = ap.parse_args()
    print(f"Connecting to {a.port} …  (Ctrl-C to quit)")
    remote = UniIr(a.port,
                   on_status=lambda up: print("● connected" if up else "● disconnected"),
                   on_log=print)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        remote.close()
        print("\nclosed.")