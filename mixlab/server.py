#!/usr/bin/env python3
"""concert-mixlab GUI server: http://127.0.0.1:8765

presets.json and state.json are the shared truth: the GUI edits them, and so can
`mixlab.py set/clone/state` (e.g. from Claude Code). The browser polls their mtimes
and re-analyses automatically when either changes.
"""
import hashlib, io, json, os, sys, threading, traceback, wave, glob, argparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mixlab as ml

CFG = ml.default_config()
LOCK = threading.Lock()
AUDIO = {}          # hash -> float master (stereo)
try:
    import plotly
    PLOTLY = os.path.join(os.path.dirname(plotly.__file__), "package_data", "plotly.min.js")
except ImportError:
    PLOTLY = None

def mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0

def versions():
    return dict(cfg=mtime(CFG), state=mtime(ml.state_path(CFG)))

def phash(preset, start, dur):
    return hashlib.sha1(json.dumps([preset, start, dur], sort_keys=True).encode()).hexdigest()[:16]

def wav16(y):
    """32-bit float WAV, so overs above 0 dBFS reach the browser unclipped and the meters can show them."""
    data = np.ascontiguousarray(y, dtype="<f4").tobytes()
    import struct
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 3, 2, ml.SR, ml.SR * 8, 8, 32) \
        + b"data" + struct.pack("<I", len(data))
    return hdr + data

def clean(o):
    if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o); return None if (f != f or f in (float("inf"), float("-inf"))) else f
    if isinstance(o, np.integer): return int(o)
    return o

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(clean(body)).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length", 0)); return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, open(os.path.join(HERE, "static/index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/plotly.js":
                if not PLOTLY:
                    return self.send(404, b"plotly not installed", "text/plain")
                return self.send(200, open(PLOTLY, "rb").read(), "application/javascript")
            if u.path == "/api/version":
                return self.send(200, versions())
            if u.path == "/api/config":
                cfg = ml.load_config(CFG); raw = json.load(open(CFG))
                return self.send(200, dict(raw=raw["presets"], resolved=cfg["resolved"], excerpts=raw.get("excerpts", {}),
                                           state=ml.read_state(CFG), version=versions(), source_dir=cfg["source_dir"]))
            if u.path == "/api/audio":
                h = q["h"]
                if h not in AUDIO:
                    cfg = ml.load_config(CFG); p = cfg["resolved"][q["preset"]]; start = ml.parse_time(q["start"]); dur = float(q["dur"])
                    with LOCK:
                        _, _, y, _ = ml.render_master(p, cfg["source_dir"], start, dur)
                    AUDIO[h] = y
                return self.send(200, wav16(AUDIO[h]), "audio/wav")
            self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc(); self.send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self.body()
            if u.path == "/api/analyze":
                cfg = ml.load_config(CFG); names = [n for n in b["presets"] if n in cfg["resolved"]]
                if not names:
                    return self.send(400, {"error": "no valid presets"})
                start = ml.parse_time(b["start"]); dur = float(b["dur"])
                with LOCK:
                    res, js = ml.analyse_many(cfg, names, start, dur, with_raw=True)
                hashes = {}
                for r in res:
                    h = phash(r["preset"], start, dur); hashes[r["name"]] = h; AUDIO[h] = r["y"]
                while len(AUDIO) > 8:
                    AUDIO.pop(next(iter(AUDIO)))
                js["audio"] = hashes
                return self.send(200, js)
            if u.path == "/api/preset":
                ml.save_preset(CFG, b["name"], b["preset"]); return self.send(200, versions())
            if u.path == "/api/delete":
                ml.delete_preset(CFG, b["name"]); return self.send(200, versions())
            if u.path == "/api/state":
                ml.write_state(CFG, **b); return self.send(200, versions())
            if u.path == "/api/ffmpeg":
                cfg = ml.load_config(CFG)
                cmd = ml.ffmpeg_command(cfg["resolved"][b["preset"]], cfg["source_dir"], ml.parse_time(b["start"]),
                                        float(b["dur"]) or None, f"{b['preset']}.wav")
                return self.send(200, {"cmd": " ".join(ml.shlex.quote(c) for c in cmd)})
            self.send(404, {"error": "not found"})
        except SystemExit as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc(); self.send(500, {"error": str(e)})

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8765); ap.add_argument("--config", default=CFG)
    a = ap.parse_args(); CFG = os.path.abspath(a.config)
    print(f"concert-mixlab GUI on http://127.0.0.1:{a.port}  (config {CFG})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
