"""Per-octave stereo correlation and side/mid of the Reason model render vs mixlab, for a preset/excerpt."""
import sys, json, math
import numpy as np
from scipy import signal
sys.path.insert(0, "/Users/petersjogren/GitHubProjects/concert-mixlab/mixlab")
import mixlab as ML, export_reason as ER

PRESET, EX = sys.argv[1], sys.argv[2]
cfg = ML.load_config(ML.default_config()); p = cfg["resolved"][PRESET]
st, du = json.load(open(ML.default_config()))["excerpts"][EX]; st = ML.parse_time(str(st)); du = float(du)
tracks, _ = ER.preset_tracks(p, cfg["source_dir"], st, du, ML.read_audio)
mx, _, _, buses = ER.map_to_reason(p, tracks)
for t in tracks:
    t["delay_samples"] = round(t["delay_ms"] * 48)
y = ER.model_render(tracks, mx, buses=buses)
_, pre, _, _ = ML.render_master(p, cfg["source_dir"], st, du)
n = min(len(y), len(pre))

def stats(x):
    f, a = signal.welch(x[:, 0], 48000, nperseg=16384); _, b = signal.welch(x[:, 1], 48000, nperseg=16384)
    _, c = signal.csd(x[:, 0], x[:, 1], 48000, nperseg=16384); out = []
    for fc in (31.5, 63, 125, 250, 500, 1000):
        k = (f >= fc / math.sqrt(2)) & (f < fc * math.sqrt(2))
        A, B, C = a[k].sum(), b[k].sum(), np.real(c[k]).sum()
        out.append((round(C / math.sqrt(A * B), 2), round(10 * math.log10((A + B - 2 * C) / (A + B + 2 * C)), 1)))
    return out

print("octaves 31.5..1k  (corr, side/mid dB)")
print("mixlab      ", stats(pre[:n]))
print("Reason model", stats(y[:n]))
