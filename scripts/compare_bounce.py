"""Compare a Reason bounce of an exported song with the reason_devices model render (and mixlab's render)."""
import sys, json, math, time
import numpy as np
from scipy import signal
sys.path.insert(0, "/Users/petersjogren/GitHubProjects/concert-mixlab/mixlab")
import mixlab as ML, export_reason as ER, reason_devices as RD

PRESET, EX, BOUNCE = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = ML.load_config(ML.default_config()); p = cfg["resolved"][PRESET]
st, du = cfg_ex = json.load(open(ML.default_config()))["excerpts"][EX]
st = ML.parse_time(str(st)); du = float(du)
tracks, _ = ER.preset_tracks(p, cfg["source_dir"], st, du, ML.read_audio)
mx, _, _ = ER.map_to_reason(p, tracks)
for t in tracks:   # start_ppq is set inside export(); recompute the same way
    t["delay_samples"] = round(t["delay_ms"] * 48)

b = ML.read_audio(BOUNCE, 0, None).astype("float64"); sr = 48000
t0 = time.time()
pre = ER.model_render(tracks, mx)                       # channel strips + maximizer input gain, no limiting
print(f"channel model {time.time()-t0:.0f}s", flush=True)
n = min(len(pre), len(b))

# align: integer lag by xcorr on the first 30 s
seg = slice(0, 30 * sr)
c = signal.correlate(b[seg, 0], pre[seg, 0], mode="full", method="fft")
lag = int(np.argmax(np.abs(c)) - (len(pre[seg]) - 1))
print("lag samples", lag, "sign", np.sign(c[np.argmax(np.abs(c))]))
bb = b[lag:lag + n] if lag >= 0 else np.vstack([np.zeros((-lag, 2)), b])[:n]
n = min(len(bb), len(pre)); bb, pre = bb[:n], pre[:n]

def report(name, ref, mask=None):
    seg = slice(10 * sr, 40 * sr)
    c = signal.correlate(bb[seg, 0], ref[seg, 0], mode="full", method="fft")
    lg = int(np.argmax(np.abs(c)) - (len(ref[seg]) - 1))
    ref = np.roll(ref, lg, axis=0)[:len(bb)]
    if len(ref) < len(bb):
        ref = np.vstack([ref, np.zeros((len(bb) - len(ref), 2))])
    print(f"   [{name}: ref shifted by {lg} samples]")
    m = np.ones(len(ref), bool) if mask is None else mask
    f, A = signal.welch(ref[m], sr, nperseg=8192, axis=0); _, B = signal.welch(bb[m], sr, nperseg=8192, axis=0)
    bands = {}
    for lo, hi in [(20, 60), (60, 250), (250, 1000), (1000, 4000), (4000, 8000), (8000, 16000), (16000, 20000)]:
        k = (f >= lo) & (f < hi)
        bands[f"{lo}-{hi}"] = [round(10 * math.log10(B[k, ch].sum() / A[k, ch].sum()), 2) for ch in (0, 1)]
    d = bb[m] - ref[m]
    print(f"== bounce vs {name}: null {10*math.log10((d**2).sum()/(ref[m]**2).sum()):.1f} dB, "
          f"level {10*math.log10((bb[m]**2).sum()/(ref[m]**2).sum()):+.3f} dB")
    print("   band diff dB [L,R]:", bands)

# 1) where the limiter is idle: model output (after maximizer gains, no limiting) below threshold
og = RD.maximizer_output_gain_linear(mx["patch_bp"][RD.MAXIMIZER_BP["Output Gain"]])
quiet = (np.abs(pre).max(axis=1) < 0.5)
win = 4800
qwin = np.repeat(np.minimum.reduceat(quiet.astype(int), np.arange(0, n, win)), win)[:n].astype(bool)
la = RD.maximizer_lookahead_samples(sr, True) + 1
pre_d = pre
print(f"limiter-idle fraction {qwin.mean():.2f}")
report("model (channel strips, maximizer gains, look-ahead delay, no limiting) — idle parts", pre_d, qwin)
report("same — whole excerpt", pre_d)
t0 = time.time()
full = RD.maximizer_process(pre / (RD.maximizer_input_gain_linear(mx["patch_bp"][1]) * og), sr, *[mx["patch_bp"][k] for k in (1,)],
                            1, 1, mx["patch_bp"][4], mx["patch_bp"][5], mx["patch_bp"][6], 0, 64)
print(f"maximizer model {time.time()-t0:.0f}s")
report("full model incl. maximizer", full[:n])
_, mpre, my, _ = ML.render_master(p, cfg["source_dir"], st, du)
my = np.vstack([np.zeros((la, 2)), my])[:n]
report("mixlab render (its own limiter)", my)
