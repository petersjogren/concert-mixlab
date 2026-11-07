#!/usr/bin/env python3
"""concert-mixlab: quantify how mix/EQ settings affect a multi-mic concert master.

Subcommands
  report   analyse one excerpt under several presets -> HTML report + metrics.json
  render   render one preset (python DSP chain) to a 24-bit WAV
  ffmpeg   print the equivalent ffmpeg command for a preset
  verify   null-test python DSP chain against ffmpeg (limiter bypassed)

The DSP chain per layer is: input routing (pan/width matrix) -> delay -> biquads -> gain.
Layers are summed, master gain applied, then a look-ahead peak limiter.
Biquads use the RBJ cookbook formulas exactly as ffmpeg's af_biquads does, so
`ffmpeg` output and the python render match (see `verify`).
"""
import argparse, base64, copy, io, json, math, os, shlex, subprocess, sys, wave
import numpy as np
from scipy import signal
from scipy.ndimage import maximum_filter1d

SR = 48000

# ----------------------------------------------------------------------------- config
def parse_time(t):
    t = str(t)
    if ":" in t:
        parts = [float(p) for p in t.split(":")]
        s = 0.0
        for p in parts:
            s = s * 60 + p
        return s
    return float(t)

def deep_merge(base, over):
    """Merge preset `over` into `base`. Layers are merged by name."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k == "extends":
            continue
        if k == "layers":
            layers = {l["name"]: l for l in out.get("layers", [])}
            order = [l["name"] for l in out.get("layers", [])]
            for l in v:
                if l["name"] in layers:
                    layers[l["name"]] = deep_merge(layers[l["name"]], l)
                else:
                    layers[l["name"]] = copy.deepcopy(l); order.append(l["name"])
            out["layers"] = [layers[n] for n in order]
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out

def load_config(path):
    cfg = json.load(open(path))
    base_dir = os.path.dirname(os.path.abspath(path))
    src = os.path.expanduser(cfg.get("source_dir", "."))
    cfg["source_dir"] = src if os.path.isabs(src) else os.path.join(base_dir, src)
    resolved = {}
    def resolve(name, stack=()):
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise SystemExit(f"preset cycle: {stack}")
        p = cfg["presets"][name]
        base = resolve(p["extends"], stack + (name,)) if "extends" in p else {}
        resolved[name] = deep_merge(base, p)
        return resolved[name]
    for n in cfg["presets"]:
        resolve(n)
    cfg["resolved"] = resolved
    return cfg

# ----------------------------------------------------------------------------- audio io
_cache = {}
def read_audio(path, start, dur):
    key = (path, start, dur)
    if key not in _cache:
        if any(k[1:] != (start, dur) for k in _cache):              # keep only one excerpt in memory
            _cache.clear(); _layer_cache.clear()
        cmd = ["ffmpeg", "-v", "error", "-ss", str(start)]
        if dur:
            cmd += ["-t", str(dur)]
        cmd += ["-i", path, "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(SR), "-"]
        ch = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                 "stream=channels", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip())
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
        _cache[key] = np.frombuffer(raw, np.float32).reshape(-1, ch).astype(np.float64)
    return _cache[key]

def write_wav24(path, x):
    x = np.clip(x, -1, 1 - 2 ** -23)
    i = np.round(x * 2 ** 23).astype(np.int32)
    b = np.zeros((i.size, 3), np.uint8)
    flat = i.reshape(-1)
    b[:, 0] = flat & 0xFF; b[:, 1] = (flat >> 8) & 0xFF; b[:, 2] = (flat >> 16) & 0xFF
    with wave.open(path, "wb") as w:
        w.setnchannels(x.shape[1]); w.setsampwidth(3); w.setframerate(SR)
        w.writeframes(b.tobytes())

# ----------------------------------------------------------------------------- filters (ffmpeg-compatible RBJ)
def biquad(f):
    t = f["type"]; w0 = 2 * math.pi * f["f"] / SR; cw, sw = math.cos(w0), math.sin(w0)
    if t == "peak":
        A = 10 ** (f["g"] / 40)
        alpha = sw * math.sinh(math.log(2) / 2 * f.get("oct", 1.0) * w0 / sw)
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]; a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif t in ("highshelf", "lowshelf"):
        A = 10 ** (f["g"] / 40); S = f.get("slope", 0.5)
        alpha = sw / 2 * math.sqrt((A + 1 / A) * (1 / S - 1) + 2); sa = 2 * math.sqrt(A) * alpha
        if t == "highshelf":
            b = [A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sa)]
            a = [(A + 1) - (A - 1) * cw + sa, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sa]
        else:
            b = [A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sa)]
            a = [(A + 1) + (A - 1) * cw + sa, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sa]
    elif t in ("highpass", "lowpass"):
        alpha = sw / (2 * f.get("q", 0.707))
        if t == "highpass":
            b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        else:
            b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    else:
        raise ValueError(f"unknown filter type {t}")
    return np.array(b + a) / a[0]

def layer_sos(layer):
    fl = [f for f in layer.get("filters", []) if not f.get("bypass")]
    return np.array([biquad(f) for f in fl]) if fl else None

def filter_to_ffmpeg(f):
    t = f["type"]
    if t == "peak":
        return f"equalizer=f={f['f']}:t=o:w={f.get('oct', 1.0)}:g={f['g']}"
    if t in ("highshelf", "lowshelf"):
        return f"{t}=f={f['f']}:g={f['g']}:t=s:w={f.get('slope', 0.5)}"
    return f"{t}=f={f['f']}:t=q:w={f.get('q', 0.707)}"

def describe_filter(f):
    t = f["type"]
    if t == "peak":
        return f"Bell {f['f']:g} Hz {f['g']:+g} dB ({f.get('oct', 1.0):g} oct)"
    if t in ("highshelf", "lowshelf"):
        return f"{'High' if t == 'highshelf' else 'Low'} shelf {f['f']:g} Hz {f['g']:+g} dB (slope {f.get('slope', 0.5):g})"
    return f"{'HPF' if t == 'highpass' else 'LPF'} {f['f']:g} Hz (Q {f.get('q', 0.707):g})"

# ----------------------------------------------------------------------------- routing
def routing_matrix(layer):
    """2 x N matrix mapping the layer's input channels to L/R, including pan + width."""
    ins = layer["inputs"]; M = np.zeros((2, len(ins)))
    for j, inp in enumerate(ins):
        p = float(inp.get("pan", 0.0)); th = (p + 1) * math.pi / 4
        g = 10 ** (inp.get("gain_db", 0.0) / 20)
        M[0, j] = math.cos(th) * g; M[1, j] = math.sin(th) * g
    w = float(layer.get("width", 1.0))
    W = np.array([[(1 + w) / 2, (1 - w) / 2], [(1 - w) / 2, (1 + w) / 2]])
    return W @ M

def db2lin(d): return 10 ** (d / 20)

# ----------------------------------------------------------------------------- render
_layer_cache = {}
def render_layers(preset, src_dir, start, dur):
    """Return dict name -> stereo array (post layer gain, pre master). Cached per layer settings."""
    outs = {}
    for layer in preset["layers"]:
        if layer.get("mute"):
            continue
        key = (json.dumps(layer, sort_keys=True), src_dir, start, dur)
        if key in _layer_cache:
            outs[layer["name"]] = _layer_cache[key]; continue
        chans = [read_audio(os.path.join(src_dir, i["file"]), start, dur)[:, i.get("ch", 0)] for i in layer["inputs"]]
        n = min(len(c) for c in chans)
        X = np.stack([c[:n] for c in chans])
        Y = (routing_matrix(layer) @ X).T
        d = int(round(layer.get("delay_ms", 0.0) * SR / 1000))
        if d > 0:
            Y = np.vstack([np.zeros((d, 2)), Y[:-d]])
        sos = layer_sos(layer)
        if sos is not None:
            Y = signal.sosfilt(sos, Y, axis=0)
        outs[layer["name"]] = Y * db2lin(layer.get("gain_db", 0.0))
        if len(_layer_cache) >= 12:
            _layer_cache.pop(next(iter(_layer_cache)))
        _layer_cache[key] = outs[layer["name"]]
    if not outs:
        raise ValueError("all layers are muted")
    n = min(len(v) for v in outs.values())
    return {k: v[:n] for k, v in outs.items()}

def limiter(x, ceiling, attack_ms=5.0, release_ms=50.0, block=48):
    """Look-ahead peak limiter (block-rate gain computer). Returns (y, gain_db_per_sample)."""
    if ceiling is None or ceiling <= 0:
        return x, np.zeros(len(x))
    la = int(attack_ms * SR / 1000)
    pk = np.max(np.abs(x), axis=1)
    env = maximum_filter1d(pk, size=2 * la + 1, origin=0)       # peak within +-lookahead
    target = np.minimum(1.0, ceiling / np.maximum(env, 1e-12))
    nb = len(x) // block + 1
    tb = np.pad(target, (0, nb * block - len(target)), constant_values=1.0).reshape(nb, block).min(1)
    rel = math.exp(-block / (release_ms * SR / 1000))
    g = np.empty(nb); cur = 1.0
    for i, t in enumerate(tb):                                    # instant attack, exp release
        cur = t if t < cur else t + (cur - t) * rel
        g[i] = cur
    gs = np.interp(np.arange(len(x)), np.arange(nb) * block + block / 2, g)
    gs = np.minimum(gs, target)                                   # never let a peak through
    return np.clip(x * gs[:, None], -ceiling, ceiling), 20 * np.log10(gs)

def render_master(preset, src_dir, start, dur):
    layers = render_layers(preset, src_dir, start, dur)
    m = preset.get("master", {})
    pre = sum(layers.values()) * db2lin(m.get("gain_db", 0.0))
    y, gr = limiter(pre, m.get("limiter_ceiling"), m.get("attack_ms", 5), m.get("release_ms", 50))
    return layers, pre, y, gr

# ----------------------------------------------------------------------------- ffmpeg equivalent
def ffmpeg_command(preset, src_dir, start, dur, out):
    files, idx = [], {}
    for layer in preset["layers"]:
        for i in layer["inputs"]:
            if i["file"] not in idx:
                idx[i["file"]] = len(files); files.append(i["file"])
    args = ["ffmpeg", "-y"]
    for f in files:
        args += ["-ss", str(start)] + (["-t", str(dur)] if dur else []) + ["-i", os.path.join(src_dir, f)]
    graph, labels = [], []
    for k, layer in enumerate(l for l in preset["layers"] if not l.get("mute")):
        streams = []
        for i in layer["inputs"]:
            s = f"[{idx[i['file']]}:a]"
            if s not in streams:
                streams.append(s)
        # flat channel index of each input inside the amerged stream
        chcount = {s: int(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=channels", "-of", "csv=p=0",
                                          os.path.join(src_dir, files[int(s[1:-3])])], capture_output=True, text=True).stdout.split()[0]) for s in streams}
        offs, o = {}, 0
        for s in streams:
            offs[s] = o; o += chcount[s]
        M = routing_matrix(layer)
        terms = [[], []]
        for j, i in enumerate(layer["inputs"]):
            c = offs[f"[{idx[i['file']]}:a]"] + i.get("ch", 0)
            for oc in (0, 1):
                if abs(M[oc, j]) > 1e-9:
                    terms[oc].append(f"{M[oc, j]:.6f}*c{c}")
        src = "".join(streams) + (f"amerge=inputs={len(streams)}," if len(streams) > 1 else "")
        chain = [f"pan=stereo|c0={'+'.join(terms[0]) or '0*c0'}|c1={'+'.join(terms[1]) or '0*c0'}"]
        if layer.get("delay_ms", 0) > 0:
            chain.append(f"adelay={layer['delay_ms']}ms|{layer['delay_ms']}ms")
        chain += [filter_to_ffmpeg(f) for f in layer.get("filters", []) if not f.get("bypass")]
        chain.append(f"volume={layer.get('gain_db', 0.0)}dB")
        graph.append(src + ",".join(chain) + f"[L{k}]"); labels.append(f"[L{k}]")
    m = preset.get("master", {})
    tail = f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,volume={m.get('gain_db', 0.0)}dB"
    if m.get("limiter_ceiling"):
        tail += f",alimiter=limit={m['limiter_ceiling']}:attack={m.get('attack_ms', 5)}:release={m.get('release_ms', 50)}"
    graph.append(tail + "[out]")
    return args + ["-filter_complex", ";".join(graph), "-map", "[out]", "-c:a", "pcm_s24le", out]

# ----------------------------------------------------------------------------- measurements
OCT = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
GUIDE = {(250, 500): 1, (2000, 4000): -7, (4000, 8000): -14, (8000, 12000): -20, (12000, 16000): -26}

def kweight(x):
    b1, a1 = [1.53512485958697, -2.69169618940638, 1.19839281085285], [1, -1.69065929318241, 0.73248077421585]
    b2, a2 = [1, -2, 1], [1, -1.99004745483398, 0.99007225036621]
    return signal.lfilter(b2, a2, signal.lfilter(b1, a1, x, axis=0), axis=0)

def loudness(x):
    k = kweight(x); blk, hop = int(0.4 * SR), int(0.1 * SR)
    p = np.array([np.sum(np.mean(k[i:i + blk] ** 2, axis=0)) for i in range(0, len(k) - blk, hop)])
    l = -0.691 + 10 * np.log10(p + 1e-20)
    g = p[l > -70]
    if not len(g):
        return -70.0, None
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g2 = p[(l > -70) & (l > rel)]
    st_blk, st_hop = 3 * SR, SR                                  # short-term (3 s) timeline
    st = [-0.691 + 10 * np.log10(np.sum(np.mean(k[i:i + st_blk] ** 2, axis=0)) + 1e-20) for i in range(0, max(1, len(k) - st_blk), st_hop)]
    return -0.691 + 10 * np.log10(g2.mean()), np.array(st)

def spectra(x, nper=8192):
    f, Pll = signal.welch(x[:, 0], SR, nperseg=nper)
    _, Prr = signal.welch(x[:, 1], SR, nperseg=nper)
    _, Plr = signal.csd(x[:, 0], x[:, 1], SR, nperseg=nper)
    return f, Pll, Prr, Plr.real

def band_mask(f, lo, hi): return (f >= lo) & (f < hi)
def octave_edges(fc): return fc / math.sqrt(2), fc * math.sqrt(2)

def stereo_by_band(f, Pll, Prr, Plr):
    rows = []
    for fc in OCT:
        m = band_mask(f, *octave_edges(fc))
        ll, rr, lr = Pll[m].sum(), Prr[m].sum(), Plr[m].sum()
        mid, side = (ll + rr + 2 * lr) / 4, (ll + rr - 2 * lr) / 4
        rows.append(dict(fc=fc, corr=lr / math.sqrt(ll * rr + 1e-30), side_mid_db=10 * math.log10((side + 1e-30) / (mid + 1e-30)),
                         balance_db=10 * math.log10((ll + 1e-30) / (rr + 1e-30))))
    return rows

def smooth_db(f, P, frac=6):
    centers = 20 * 2 ** (np.arange(0, 10.2 * frac) / frac)
    centers = centers[centers < SR / 2 * 0.95]
    out = []
    for c in centers:
        m = band_mask(f, c * 2 ** (-0.5 / frac), c * 2 ** (0.5 / frac))
        out.append(10 * np.log10(P[m].mean() + 1e-30) if m.any() else np.nan)
    return centers, np.array(out)

def rel_level(f, P, lo, hi, ref=(500, 2000)):
    return 10 * np.log10(P[band_mask(f, lo, hi)].mean() / P[band_mask(f, *ref)].mean())

def gcc_phat(u, w, maxlag=int(0.05 * SR)):
    n = len(u) + len(w); R = np.fft.rfft(w, n) * np.conj(np.fft.rfft(u, n)); R /= np.abs(R) + 1e-12
    cc = np.fft.irfft(R, n); cc = np.concatenate([cc[-maxlag:], cc[:maxlag + 1]]); i = int(np.argmax(cc))
    return i - maxlag, float(cc[i])

def alignment(preset, src_dir, start, dur, win=10):
    """GCC-PHAT lag of every input channel of later layers vs every input of the first layer.
    Median over `win`-second windows; spread (IQR) tells how trustworthy it is."""
    def chans(layer):
        return [(f"{i['file']}[{i.get('ch', 0)}]", read_audio(os.path.join(src_dir, i["file"]), start, dur)[:, i.get("ch", 0)])
                for i in layer["inputs"]]
    ref_layer = preset["layers"][0]; rows = []
    for layer in preset["layers"][1:]:
        for rn, r in chans(ref_layer):
            for cn, c in chans(layer):
                n = min(len(r), len(c)); lags, peaks = [], []
                for s in range(0, n - win * SR + 1, win * SR):
                    lag, pk = gcc_phat(r[s:s + win * SR], c[s:s + win * SR]); lags.append(lag); peaks.append(pk)
                if lags:
                    q1, q3 = np.percentile(lags, [25, 75])
                    rows.append(dict(ref=f"{ref_layer['name']}: {rn}", layer=f"{layer['name']}: {cn}",
                                     lag_ms=1000 * float(np.median(lags)) / SR, spread_ms=1000 * float(q3 - q1) / SR,
                                     confidence=float(np.median(peaks)), windows=len(lags)))
    return rows

def analyse(name, preset, src_dir, start, dur):
    layers, pre, y, gr = render_master(preset, src_dir, start, dur)
    f, Pll, Prr, Plr = spectra(y)
    Pm = (Pll + Prr) / 2
    lufs, st = loudness(y)
    # per-layer contribution (pre-limiter) and coherent summation gain
    contrib = {}
    lspec = {}
    for k, v in layers.items():
        _, a, b, _ = spectra(v); lspec[k] = a + b
    _, a, b, _ = spectra(sum(layers.values())); mixP = a + b      # layer sum without master gain
    band_rows = []
    for fc in OCT:
        m = band_mask(f, *octave_edges(fc))
        e = {k: float(P[m].sum()) for k, P in lspec.items()}; tot = sum(e.values()) + 1e-30
        band_rows.append(dict(fc=fc, share={k: v / tot for k, v in e.items()},
                              sum_gain_db=10 * math.log10(mixP[m].sum() / tot)))
    full = {k: float(P[band_mask(f, 40, 20000)].sum()) for k, P in lspec.items()}; tot = sum(full.values())
    corr = float(np.sum(Plr) / math.sqrt(np.sum(Pll) * np.sum(Prr)))
    return dict(
        name=name, preset=preset, f=f, P=Pm, stereo=stereo_by_band(f, Pll, Prr, Plr), contrib=band_rows,
        contrib_total={k: v / tot for k, v in full.items()}, st=st, gr=gr, y=y,
        metrics=dict(lufs=lufs, peak_dbfs=20 * math.log10(np.max(np.abs(y)) + 1e-12),
                     crest_db=20 * math.log10(np.max(np.abs(y)) / (np.sqrt(np.mean(y ** 2)) + 1e-12)),
                     corr=corr, side_mid_db=10 * math.log10(max(np.sum(Pll + Prr - 2 * Plr), 1e-30) / np.sum(Pll + Prr + 2 * Plr)),
                     balance_db=10 * math.log10(np.sum(Pll) / np.sum(Prr)),
                     gr_max_db=float(-gr.min()), gr_time_pct_over_1db=float(100 * np.mean(gr < -1)),
                     centroid_hz=float(np.sum(f * Pm * band_mask(f, 40, 20000)) / np.sum(Pm * band_mask(f, 40, 20000)))),
        tilt={f"{lo}-{hi}": rel_level(f, Pm, lo, hi) for lo, hi in
              [(60, 120), (120, 250), (250, 500), (2000, 4000), (4000, 8000), (8000, 12000), (12000, 16000), (16000, 20000)]})

# ----------------------------------------------------------------------------- plotting
def fig_png(fig):
    import matplotlib.pyplot as plt
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110, bbox_inches="tight"); plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()

def make_plots(results, raw, src_dir):
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.3})
    cols = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    P = {}
    def logx(ax):
        ax.set_xscale("log"); ax.set_xlim(20, 22000)
        ax.set_xticks([31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000])
        ax.set_xticklabels(["31", "63", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"])
    # EQ curves
    lnames = list(dict.fromkeys(l["name"] for r in results for l in r["preset"]["layers"]))
    fig, axs = plt.subplots(1, len(lnames), figsize=(5.5 * len(lnames), 3.4), squeeze=False)
    w = np.logspace(math.log10(20), math.log10(22000), 600)
    for ax, ln in zip(axs[0], lnames):
        for c, r in enumerate(results):
            L = next((l for l in r["preset"]["layers"] if l["name"] == ln), None)
            if L is None or L.get("mute"):
                continue
            sos = layer_sos(L); h = np.ones_like(w, dtype=complex)
            if sos is not None:
                _, h = signal.sosfreqz(sos, worN=w, fs=SR)
            ax.plot(w, 20 * np.log10(np.abs(h)) + L.get("gain_db", 0), color=cols[c % 10], label=r["name"])
        logx(ax); ax.set_title(f"EQ + gain: layer '{ln}'"); ax.set_ylabel("dB"); ax.legend()
    P["eq"] = fig_png(fig)
    # spectra
    fig, axs = plt.subplots(2, 1, figsize=(11, 7.5))
    for c, (nm, (f, Pr)) in enumerate(raw.items()):
        x, d = smooth_db(f, Pr); ref = 10 * np.log10(Pr[band_mask(f, 500, 2000)].mean())
        axs[0].plot(x, d - ref, color=cols[c % 10], label=nm)
    axs[0].set_title("Raw inputs (1/6-oct, 0 dB = 0.5–2 kHz mean)")
    for c, r in enumerate(results):
        x, d = smooth_db(r["f"], r["P"]); ref = 10 * np.log10(r["P"][band_mask(r["f"], 500, 2000)].mean())
        axs[1].plot(x, d - ref, color=cols[c % 10], label=r["name"])
    for (lo, hi), g in GUIDE.items():
        axs[1].hlines(g, lo, hi, colors="k", linestyles="dashed", lw=1.5)
    axs[1].plot([], [], "k--", label="rough guide")
    axs[1].set_title("Master per preset (same normalisation)")
    for ax in axs:
        logx(ax); ax.set_ylim(-70, 15); ax.set_ylabel("dB"); ax.legend(fontsize=8)
    P["spectra"] = fig_png(fig)
    # delta vs first preset
    if len(results) > 1:
        fig, ax = plt.subplots(figsize=(11, 3.4)); r0 = results[0]
        x0, d0 = smooth_db(r0["f"], r0["P"]); ref0 = 10 * np.log10(r0["P"][band_mask(r0["f"], 500, 2000)].mean())
        for c, r in enumerate(results[1:], 1):
            x, d = smooth_db(r["f"], r["P"]); ref = 10 * np.log10(r["P"][band_mask(r["f"], 500, 2000)].mean())
            ax.plot(x, (d - ref) - (d0 - ref0), color=cols[c % 10], label=f"{r['name']} − {r0['name']}")
        logx(ax); ax.axhline(0, color="k", lw=0.8); ax.set_ylabel("dB"); ax.legend()
        ax.set_title(f"Tonal change relative to '{r0['name']}' (loudness-normalised at 0.5–2 kHz)")
        P["delta"] = fig_png(fig)
    # contribution
    fig, axs = plt.subplots(1, len(results), figsize=(4.2 * len(results), 3.4), squeeze=False, sharey=True)
    for ax, r in zip(axs[0], results):
        bottom = np.zeros(len(OCT)); lab = [f"{fc:g}" if fc < 1000 else f"{fc / 1000:g}k" for fc in OCT]
        for c, ln in enumerate(lnames):
            v = np.array([b["share"].get(ln, 0) * 100 for b in r["contrib"]])
            ax.bar(lab, v, bottom=bottom, color=cols[c % 10], label=ln); bottom += v
        ax2 = ax.twinx(); ax2.plot(lab, [b["sum_gain_db"] for b in r["contrib"]], "k.-", lw=1)
        ax2.set_ylim(-6, 6); ax2.axhline(0, color="k", lw=0.5, ls=":")
        ax2.set_ylabel("sum gain dB (+ coherent, − cancel)", fontsize=7)
        ax.set_title(r["name"]); ax.set_ylim(0, 100); ax.tick_params(axis="x", rotation=60)
    axs[0][0].set_ylabel("% of master energy"); axs[0][0].legend(fontsize=7, loc="lower left")
    P["contrib"] = fig_png(fig)
    # stereo by band
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.4))
    for c, r in enumerate(results):
        axs[0].semilogx(OCT, [s["corr"] for s in r["stereo"]], "o-", color=cols[c % 10], label=r["name"])
        axs[1].semilogx(OCT, [s["side_mid_db"] for s in r["stereo"]], "o-", color=cols[c % 10], label=r["name"])
    axs[0].set_title("L/R correlation per octave (1 = mono, 0 = wide/diffuse, <0 = phasey)"); axs[0].set_ylim(-0.5, 1.05)
    axs[1].set_title("Side/Mid energy per octave (dB; 0 = very wide, −∞ = mono)")
    for ax in axs:
        ax.set_xticks(OCT); ax.set_xticklabels([f"{x:g}" if x < 1000 else f"{x / 1000:g}k" for x in OCT]); ax.legend(fontsize=8)
    P["stereo"] = fig_png(fig)
    # vectorscope
    fig, axs = plt.subplots(1, len(results), figsize=(3.3 * len(results), 3.4), squeeze=False)
    for ax, r in zip(axs[0], results):
        y = r["y"][::7]; rms = np.sqrt(np.mean(y ** 2)) + 1e-12
        Mi, Si = (y[:, 0] + y[:, 1]) / math.sqrt(2) / rms, (y[:, 0] - y[:, 1]) / math.sqrt(2) / rms
        ax.hist2d(Si, Mi, bins=161, range=[[-5, 5], [-5, 5]], cmap="magma", norm=matplotlib.colors.LogNorm())
        ax.set_aspect("equal"); ax.set_title(f"{r['name']}\ncorr {r['metrics']['corr']:.2f}"); ax.set_xlabel("Side"); ax.set_ylabel("Mid")
        ax.grid(False)
    P["vector"] = fig_png(fig)
    # loudness + limiter timeline
    fig, axs = plt.subplots(2, 1, figsize=(11, 5), sharex=True)
    for c, r in enumerate(results):
        axs[0].plot(np.arange(len(r["st"])), r["st"], color=cols[c % 10], label=r["name"])
        g = r["gr"]; nb = len(g) // SR
        axs[1].plot(np.arange(nb), g[:nb * SR].reshape(nb, SR).min(1), color=cols[c % 10], label=r["name"])
    axs[0].set_ylabel("short-term LUFS"); axs[0].legend(fontsize=8); axs[1].set_ylabel("limiter gain (dB)"); axs[1].set_xlabel("seconds")
    P["timeline"] = fig_png(fig)
    return P

# ----------------------------------------------------------------------------- report
CSS = """body{font-family:-apple-system,Helvetica,sans-serif;max-width:1180px;margin:20px auto;padding:0 16px;color:#222}
table{border-collapse:collapse;margin:8px 0 18px;font-size:13px}td,th{border:1px solid #ccc;padding:3px 8px;text-align:right}
th{background:#f2f2f2}td.l,th.l{text-align:left}img{max-width:100%}h2{border-bottom:2px solid #444;padding-bottom:2px;margin-top:34px}
code,pre{background:#f6f6f6;font-size:12px}pre{padding:8px;white-space:pre-wrap;word-break:break-all}.note{color:#666;font-size:13px}
.good{background:#e3f6e3}.warn{background:#fdf0d5}.bad{background:#fadddd}"""

def html_table(head, rows, left=1):
    h = "".join(f"<th class='{'l' if i < left else ''}'>{x}</th>" for i, x in enumerate(head))
    b = "".join("<tr>" + "".join(f"<td class='{'l' if i < left else ''}'>{x}</td>" for i, x in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><tr>{h}</tr>{b}</table>"

def guide_class(band, v):
    for (lo, hi), g in GUIDE.items():
        if band == f"{lo}-{hi}":
            d = abs(v - g)
            return "good" if d <= 3 else "warn" if d <= 7 else "bad"
    return ""

def build_report(results, raw, align, args, cfg, plots):
    H = [f"<html><head><meta charset='utf-8'><title>mixlab report</title><style>{CSS}</style></head><body>",
         f"<h1>concert-mixlab report</h1><p class='note'>Excerpt {args.start} + {args.dur}s &nbsp;·&nbsp; source: {cfg['source_dir']}"
         f" &nbsp;·&nbsp; presets: {', '.join(r['name'] for r in results)}</p>"]
    # settings
    H.append("<h2>1. Settings</h2>")
    for r in results:
        p = r["preset"]; rows = []
        for L in p["layers"]:
            ins = ", ".join(f"{i['file']}[{i.get('ch', 0)}] pan {i.get('pan', 0):+g}" for i in L["inputs"])
            fl = "<br>".join(describe_filter(f) + (" <i>(bypassed)</i>" if f.get("bypass") else "") for f in L.get("filters", [])) or "—"
            rows.append([L["name"] + (" <b>(muted)</b>" if L.get("mute") else ""), ins, f"{L.get('width', 1.0):g}",
                         f"{L.get('delay_ms', 0):g}", fl, f"{L.get('gain_db', 0):+.1f}"])
        m = p.get("master", {})
        H.append(f"<h3>{r['name']}</h3><p class='note'>{p.get('description', '')}</p>")
        H.append(html_table(["layer", "inputs", "width", "delay ms", "filters", "gain dB"], rows, left=5))
        H.append(f"<p class='note'>Master: gain {m.get('gain_db', 0):+.1f} dB, limiter ceiling {m.get('limiter_ceiling', 'off')}"
                 f" (attack {m.get('attack_ms', 5)} ms, release {m.get('release_ms', 50)} ms)</p>")
    # summary metrics
    H.append("<h2>2. Master summary</h2>")
    rows = []
    for r in results:
        m = r["metrics"]
        rows.append([r["name"], f"{m['lufs']:.1f}", f"{m['peak_dbfs']:.1f}", f"{m['crest_db']:.1f}", f"{m['centroid_hz']:.0f}",
                     f"{m['corr']:.2f}", f"{m['side_mid_db']:.1f}", f"{m['balance_db']:+.1f}", f"{m['gr_max_db']:.1f}",
                     f"{m['gr_time_pct_over_1db']:.1f}", " / ".join(f"{k} {v * 100:.0f}%" for k, v in r["contrib_total"].items())])
    H.append(html_table(["preset", "LUFS", "peak dBFS", "crest dB", "centroid Hz", "L/R corr", "S/M dB", "L−R dB",
                         "max GR dB", "% time GR>1dB", "energy share"], rows))
    H.append("<p class='note'>Centroid ↑ = brighter. Correlation 0.3–0.7 is typical for a natural orchestral image; "
             "S/M around −6…−3 dB is wide but stable. Large GR or high % time means the limiter is squashing dynamics.</p>")
    # tonal table
    H.append("<h2>3. Spectrum / EQ</h2><img src='data:image/png;base64," + plots["eq"] + "'>")
    bands = list(results[0]["tilt"].keys())
    rows = [[r["name"]] + [f"<span>{r['tilt'][b]:+.1f}</span>" for b in bands] for r in results]
    rows = []
    for r in results:
        rows.append([r["name"]] + [r["tilt"][b] for b in bands])
    g = [(f"{lo}-{hi}", v) for (lo, hi), v in GUIDE.items()]
    trs = "".join("<tr><td class='l'>" + row[0] + "</td>" + "".join(
        f"<td class='{guide_class(b, v)}'>{v:+.1f}</td>" for b, v in zip(bands, row[1:])) + "</tr>" for row in rows)
    guide_row = "<tr><td class='l'><i>rough guide</i></td>" + "".join(
        f"<td><i>{dict(g).get(b, ''):+}</i></td>" if b in dict(g) else "<td></td>" for b in bands) + "</tr>"
    H.append("<table><tr><th class='l'>band (Hz), dB rel 0.5–2 kHz</th>" + "".join(f"<th>{b}</th>" for b in bands) + f"</tr>{trs}{guide_row}</table>")
    H.append("<p class='note'>Green = within 3 dB of the guide, amber within 7 dB, red further away. The guide is a rough "
             "rule of thumb for an open concert-hall orchestral balance, not a standard.</p>")
    H.append("<img src='data:image/png;base64," + plots["spectra"] + "'>")
    if "delta" in plots:
        H.append("<img src='data:image/png;base64," + plots["delta"] + "'>")
    # mic usage
    H.append("<h2>4. Mic usage</h2><p class='note'>Bars: share of master energy (pre-limiter) per octave from each layer. "
             "Black line: how much louder the actual sum is than the energy sum. +3 dB = layers fully coherent "
             "(in phase), 0 = uncorrelated, negative = phase cancellation / comb filtering, which a delay fix should reduce.</p>")
    H.append("<img src='data:image/png;base64," + plots["contrib"] + "'>")
    if align:
        H.append("<h3>Time alignment of raw layers (GCC-PHAT, median over 10 s windows)</h3>")
        H.append(html_table(["reference", "input", "lag ms (+ = input later)", "spread ms (IQR)", "confidence", "windows"],
                            [[a["ref"], a["layer"], f"{a['lag_ms']:+.2f}", f"{a['spread_ms']:.2f}", f"{a['confidence']:.3f}", a["windows"]] for a in align], left=2))
        H.append("<p class='note'>Small spread and confidence clearly above ~0.02 mean the measurement is trustworthy. "
                 "If the far mic is later, delay the closer layer by that amount to line them up.</p>")
    # stereo
    H.append("<h2>5. Stereo image</h2><img src='data:image/png;base64," + plots["stereo"] + "'>")
    H.append("<img src='data:image/png;base64," + plots["vector"] + "'>")
    H.append("<p class='note'>Vectorscope: vertical = mono/centre content, horizontal spread = width. A round blob is a wide, "
             "diffuse image; a vertical line is mono; a horizontal smear means lots of out-of-phase content.</p>")
    # loudness
    H.append("<h2>6. Loudness &amp; limiter over time</h2><img src='data:image/png;base64," + plots["timeline"] + "'>")
    # ffmpeg
    H.append("<h2>7. Reproduce with ffmpeg</h2>")
    for r in results:
        cmd = ffmpeg_command(r["preset"], cfg["source_dir"], parse_time(args.start), args.dur, f"{r['name']}.wav")
        H.append(f"<h3>{r['name']}</h3><pre>{' '.join(shlex.quote(c) for c in cmd)}</pre>")
    H.append("</body></html>")
    return "\n".join(H)

def jsonable(r):
    return dict(name=r["name"], metrics=r["metrics"], tilt=r["tilt"], stereo=r["stereo"], contrib=r["contrib"],
                contrib_total=r["contrib_total"], preset=r["preset"])

# ----------------------------------------------------------------------------- JSON API (GUI + agents)
def eq_curve(layer, n=300):
    w = np.logspace(math.log10(20), math.log10(22000), n); sos = layer_sos(layer)
    h = np.ones_like(w, dtype=complex) if sos is None else signal.sosfreqz(sos, worN=w, fs=SR)[1]
    return w, 20 * np.log10(np.abs(h) + 1e-12) + layer.get("gain_db", 0.0)

def norm_smooth(f, P):
    x, d = smooth_db(f, P); ref = 10 * np.log10(P[band_mask(f, 500, 2000)].mean())
    ok = ~np.isnan(d)
    return x[ok].round(1).tolist(), (d[ok] - ref).round(2).tolist()

def raw_spectra(preset, src_dir, start, dur):
    out = []
    for L in preset["layers"]:
        for i in L["inputs"]:
            x = read_audio(os.path.join(src_dir, i["file"]), start, dur)[:, i.get("ch", 0)]
            f, p = signal.welch(x, SR, nperseg=8192); fx, d = norm_smooth(f, p)
            out.append(dict(name=f"{L['name']}: {i['file']}[{i.get('ch', 0)}]", f=fx, db=d))
    return out

def analysis_json(results, raw=None, align=None):
    """Plot-ready, JSON-serialisable summary of analyse() results."""
    lnames = list(dict.fromkeys(l["name"] for r in results for l in r["preset"]["layers"]))
    eq = {ln: {} for ln in lnames}
    for r in results:
        for L in r["preset"]["layers"]:
            if not L.get("mute"):
                w, d = eq_curve(L); eq[L["name"]][r["name"]] = dict(f=w.round(1).tolist(), db=d.round(2).tolist())
    out = dict(oct=OCT, guide=[[lo, hi, g] for (lo, hi), g in GUIDE.items()], layers=lnames, eq=eq, raw=raw or [], align=align or [],
               presets=[])
    for r in results:
        fx, d = norm_smooth(r["f"], r["P"])
        y = r["y"][::7]; rms = np.sqrt(np.mean(y ** 2)) + 1e-12
        Mi, Si = (y[:, 0] + y[:, 1]) / math.sqrt(2) / rms, (y[:, 0] - y[:, 1]) / math.sqrt(2) / rms
        H, _, _ = np.histogram2d(Mi, Si, bins=81, range=[[-5, 5], [-5, 5]])
        g = r["gr"]; nb = len(g) // SR
        out["presets"].append(dict(
            name=r["name"], metrics=r["metrics"], tilt=r["tilt"], contrib_total=r["contrib_total"],
            spectrum=dict(f=fx, db=d),
            contrib=dict(share={ln: [b["share"].get(ln, 0.0) for b in r["contrib"]] for ln in lnames},
                         sum_gain=[b["sum_gain_db"] for b in r["contrib"]]),
            stereo=dict(corr=[s["corr"] for s in r["stereo"]], sm=[s["side_mid_db"] for s in r["stereo"]],
                        balance=[s["balance_db"] for s in r["stereo"]]),
            vector=np.log10(H + 1).round(2).tolist(),
            st=[round(float(v), 2) for v in r["st"]],
            gr=g[:nb * SR].reshape(nb, SR).min(1).round(2).tolist() if nb else []))
    return out

def analyse_many(cfg, names, start, dur, with_raw=True):
    res = [analyse(n, cfg["resolved"][n], cfg["source_dir"], start, dur) for n in names]
    raw = raw_spectra(cfg["resolved"][names[0]], cfg["source_dir"], start, dur) if with_raw else None
    align = alignment(cfg["resolved"][names[0]], cfg["source_dir"], start, dur) if with_raw else None
    return res, analysis_json(res, raw, align)

# --- config editing (flattening: an edited preset no longer `extends` anything)
def save_preset(cfg_path, name, preset):
    raw = json.load(open(cfg_path))
    p = copy.deepcopy(preset); p.pop("extends", None)
    raw["presets"][name] = p
    tmp = cfg_path + ".tmp"; json.dump(raw, open(tmp, "w"), indent=2); os.replace(tmp, cfg_path)

def delete_preset(cfg_path, name):
    raw = json.load(open(cfg_path))
    users = [n for n, p in raw["presets"].items() if p.get("extends") == name]
    if users:
        raise SystemExit(f"cannot delete '{name}': extended by {users}")
    raw["presets"].pop(name)
    json.dump(raw, open(cfg_path, "w"), indent=2)

def set_path(preset, path, value):
    """path like layers.close.filters.3.g ; layers are addressed by name, lists by index."""
    keys = path.split("."); obj = preset
    for i, k in enumerate(keys):
        last = i == len(keys) - 1
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and "name" in obj[0] and not k.lstrip("-").isdigit():
                idx = next((j for j, e in enumerate(obj) if e["name"] == k), None)
                if idx is None:
                    raise SystemExit(f"no element named '{k}' in {path}")
            else:
                idx = int(k)
                if idx == len(obj) and last:
                    obj.append(None)
            if last:
                if value is None:
                    obj.pop(idx)
                else:
                    obj[idx] = value
            else:
                obj = obj[idx]
        else:
            if last:
                if value is None:
                    obj.pop(k, None)
                else:
                    obj[k] = value
            else:
                obj = obj.setdefault(k, {})

def parse_value(s):
    if s in ("null", "delete"):
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

STATE_FILE = "state.json"
def state_path(cfg_path): return os.path.join(os.path.dirname(os.path.abspath(cfg_path)), STATE_FILE)
def read_state(cfg_path):
    try:
        return json.load(open(state_path(cfg_path)))
    except (OSError, json.JSONDecodeError):
        return {}
def write_state(cfg_path, **kw):
    s = read_state(cfg_path); s.update({k: v for k, v in kw.items() if v is not None})
    json.dump(s, open(state_path(cfg_path), "w"), indent=2); return s

# ----------------------------------------------------------------------------- commands
def cmd_report(args):
    cfg = load_config(args.config); start = parse_time(args.start)
    names = args.presets.split(",") if args.presets else list(cfg["presets"])
    os.makedirs(args.out, exist_ok=True)
    results = []
    for n in names:
        print(f"analysing preset '{n}' ...", flush=True)
        results.append(analyse(n, cfg["resolved"][n], cfg["source_dir"], start, args.dur))
        if args.render:
            write_wav24(os.path.join(args.out, f"{n}.wav"), results[-1]["y"])
    # raw input spectra (every input channel used by the first preset, mono)
    raw = {}
    for L in cfg["resolved"][names[0]]["layers"]:
        for i in L["inputs"]:
            x = read_audio(os.path.join(cfg["source_dir"], i["file"]), start, args.dur)[:, i.get("ch", 0)]
            f, p = signal.welch(x, SR, nperseg=8192); raw[f"{L['name']}: {i['file']}[{i.get('ch', 0)}]"] = (f, p)
    align = alignment(cfg["resolved"][names[0]], cfg["source_dir"], start, args.dur)
    plots = make_plots(results, raw, cfg["source_dir"])
    html = build_report(results, raw, align, args, cfg, plots)
    open(os.path.join(args.out, "report.html"), "w").write(html)
    json.dump(dict(start=args.start, dur=args.dur, alignment=align, presets=[jsonable(r) for r in results]),
              open(os.path.join(args.out, "metrics.json"), "w"), indent=1, default=float)
    print("\n" + "preset".ljust(16) + "LUFS   peak  centroid  corr  S/M   GR>1dB%  " + "  ".join(f"{b:>11}" for b in list(results[0]["tilt"])[2:7]))
    for r in results:
        m = r["metrics"]
        print(r["name"].ljust(16) + f"{m['lufs']:5.1f} {m['peak_dbfs']:5.1f} {m['centroid_hz']:8.0f} {m['corr']:5.2f} {m['side_mid_db']:5.1f} {m['gr_time_pct_over_1db']:7.1f}  "
              + "  ".join(f"{r['tilt'][b]:11.1f}" for b in list(r["tilt"])[2:7]))
    print(f"\nreport: {os.path.abspath(os.path.join(args.out, 'report.html'))}")

def cmd_render(args):
    cfg = load_config(args.config)
    _, _, y, gr = render_master(cfg["resolved"][args.preset], cfg["source_dir"], parse_time(args.start), args.dur)
    if args.fade:
        n = int(args.fade * SR); ramp = np.linspace(0, 1, n)[:, None]; y[:n] *= ramp; y[-n:] *= ramp[::-1]
    write_wav24(args.out, y); print(f"wrote {args.out}  LUFS {loudness(y)[0]:.1f}  max GR {-gr.min():.1f} dB")

def cmd_ffmpeg(args):
    cfg = load_config(args.config)
    print(" ".join(shlex.quote(c) for c in ffmpeg_command(cfg["resolved"][args.preset], cfg["source_dir"], parse_time(args.start), args.dur, args.out)))

def cmd_verify(args):
    cfg = load_config(args.config); p = copy.deepcopy(cfg["resolved"][args.preset]); p.setdefault("master", {})["limiter_ceiling"] = None
    start = parse_time(args.start)
    _, pre, _, _ = render_master(p, cfg["source_dir"], start, args.dur)
    cmd = ffmpeg_command(p, cfg["source_dir"], start, args.dur, "-")
    cmd[cmd.index("pcm_s24le")] = "pcm_f32le"; cmd[-1:] = ["-f", "f32le", "-"]
    ff = np.frombuffer(subprocess.run(cmd[:1] + ["-v", "error"] + cmd[1:], capture_output=True, check=True).stdout, np.float32).reshape(-1, 2)
    n = min(len(ff), len(pre)); d = pre[:n] - ff[:n]
    res = 10 * np.log10(np.mean(d ** 2) / np.mean(pre[:n] ** 2))
    print(f"python vs ffmpeg residual: {res:.1f} dB relative to signal ({'PASS' if res < -60 else 'FAIL'})")

def cmd_list(args):
    cfg = load_config(args.config); raw = json.load(open(args.config)); st = read_state(args.config)
    for n, p in cfg["resolved"].items():
        ext = raw["presets"][n].get("extends")
        print(f"{n:16s} {'(extends ' + ext + ')' if ext else '':22s} {p.get('description', '')}")
    print("\nexcerpts:", json.dumps(raw.get("excerpts", {})))
    print("gui state:", json.dumps(st))

def cmd_show(args):
    cfg = load_config(args.config); p = cfg["resolved"][args.preset]
    if args.ffmpeg_style:
        for L in p["layers"]:
            print(f"[{L['name']}]{' MUTED' if L.get('mute') else ''} width {L.get('width', 1)} delay {L.get('delay_ms', 0)} ms gain {L.get('gain_db', 0):+} dB")
            for j, f in enumerate(L.get("filters", [])):
                print(f"   filters.{j}: {describe_filter(f)}{' (bypass)' if f.get('bypass') else ''}")
        print(f"[master] {json.dumps(p.get('master', {}))}")
    else:
        print(json.dumps(p, indent=2))

def cmd_set(args):
    cfg = load_config(args.config)
    if args.preset not in cfg["resolved"]:
        raise SystemExit(f"unknown preset {args.preset}; use clone first")
    p = copy.deepcopy(cfg["resolved"][args.preset])
    for a in args.assign:
        k, _, v = a.partition("=")
        set_path(p, k.strip(), parse_value(v.strip()))
    save_preset(args.config, args.preset, p)
    print(f"saved '{args.preset}'"); args.ffmpeg_style = True; cmd_show(args)

def cmd_clone(args):
    cfg = load_config(args.config)
    p = copy.deepcopy(cfg["resolved"][args.src])
    if args.description:
        p["description"] = args.description
    save_preset(args.config, args.dst, p); print(f"cloned '{args.src}' -> '{args.dst}'")

def cmd_delete(args):
    delete_preset(args.config, args.preset); print(f"deleted '{args.preset}'")

def cmd_metrics(args):
    """Compact JSON for agents: metrics, tonal tilt, mic share, stereo per octave, alignment."""
    cfg = load_config(args.config); names = args.presets.split(",") if args.presets else list(cfg["presets"])
    res, js = analyse_many(cfg, names, parse_time(args.start), args.dur, with_raw=args.align)
    out = dict(excerpt=dict(start=args.start, dur=args.dur), guide={f"{lo}-{hi}": g for lo, hi, g in js["guide"]},
               octaves=OCT, alignment=js["align"], presets={})
    for p in js["presets"]:
        out["presets"][p["name"]] = dict(
            metrics={k: round(v, 3) for k, v in p["metrics"].items()},
            tilt_rel_0p5_2k={k: round(v, 1) for k, v in p["tilt"].items()},
            energy_share={k: round(v, 3) for k, v in p["contrib_total"].items()},
            stereo_corr_per_octave=[round(v, 2) for v in p["stereo"]["corr"]],
            side_mid_db_per_octave=[round(v, 1) for v in p["stereo"]["sm"]],
            layer_sum_gain_db_per_octave=[round(v, 2) for v in p["contrib"]["sum_gain"]])
    print(json.dumps(out, indent=1))

def cmd_state(args):
    """Read or change what the GUI shows (excerpt, compared presets, edited preset). The GUI follows within ~2 s."""
    kw = {}
    if args.set_start is not None: kw["start"] = args.set_start
    if args.set_dur is not None: kw["dur"] = args.set_dur
    if args.compare: kw["compare"] = args.compare.split(",")
    if args.edit: kw["edit"] = args.edit
    if args.excerpt:
        ex = json.load(open(args.config)).get("excerpts", {})[args.excerpt]; kw["start"], kw["dur"] = ex[0], ex[1]
    print(json.dumps(write_state(args.config, **kw) if kw else read_state(args.config), indent=2))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    default_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")
    def cfgarg(p): p.add_argument("--config", default=default_cfg)
    def common(p, preset_many=False):
        cfgarg(p)
        p.add_argument("--start", default=None, help="seconds or mm:ss (default: GUI state.json, else 0)")
        p.add_argument("--dur", type=float, default=None, help="seconds, 0 = to end (default: GUI state.json, else 60)")
        if preset_many:
            p.add_argument("--presets", help="comma list (default: all)")
        else:
            p.add_argument("--preset", required=True)
    p = sp.add_parser("report", help="HTML report"); common(p, True); p.add_argument("--out", default="out"); p.add_argument("--render", action="store_true", help="also write each preset's master WAV")
    p = sp.add_parser("render", help="render one preset to WAV"); common(p); p.add_argument("--out", required=True); p.add_argument("--fade", type=float, default=0)
    p = sp.add_parser("ffmpeg", help="print equivalent ffmpeg command"); common(p); p.add_argument("--out", default="out.wav")
    p = sp.add_parser("verify", help="python vs ffmpeg null test"); common(p)
    p = sp.add_parser("metrics", help="compact JSON analysis (for agents)"); common(p, True); p.add_argument("--align", action="store_true", help="include GCC-PHAT alignment (slower)")
    p = sp.add_parser("list", help="list presets, excerpts, GUI state"); cfgarg(p)
    p = sp.add_parser("show", help="show resolved preset"); cfgarg(p); p.add_argument("--preset", required=True); p.add_argument("--short", dest="ffmpeg_style", action="store_true")
    p = sp.add_parser("set", help="edit preset params: path=value ..."); cfgarg(p); p.add_argument("--preset", required=True)
    p.add_argument("assign", nargs="+", help="e.g. layers.close.width=0.7  layers.close.filters.3.g=12  layers.camera.mute=true  "
                                             "layers.close.filters.4='{\"type\":\"peak\",\"f\":8000,\"oct\":1,\"g\":2}'  layers.close.filters.2=delete")
    p = sp.add_parser("clone", help="copy a preset"); cfgarg(p); p.add_argument("src"); p.add_argument("dst"); p.add_argument("--description")
    p = sp.add_parser("delete", help="delete a preset"); cfgarg(p); p.add_argument("--preset", required=True)
    p = sp.add_parser("state", help="read/set GUI state"); cfgarg(p)
    p.add_argument("--start", dest="set_start"); p.add_argument("--dur", dest="set_dur", type=float)
    p.add_argument("--excerpt", help="name from presets.json 'excerpts'"); p.add_argument("--compare", help="comma list of presets"); p.add_argument("--edit")
    a = ap.parse_args()
    if hasattr(a, "dur") and a.cmd != "state":
        st = read_state(a.config)
        if a.start is None: a.start = str(st.get("start", "0"))
        if a.dur is None: a.dur = float(st.get("dur", 60.0))
        if a.dur == 0: a.dur = None
    dict(report=cmd_report, render=cmd_render, ffmpeg=cmd_ffmpeg, verify=cmd_verify, metrics=cmd_metrics, list=cmd_list,
         show=cmd_show, set=cmd_set, clone=cmd_clone, delete=cmd_delete, state=cmd_state)[a.cmd](a)

if __name__ == "__main__":
    main()
