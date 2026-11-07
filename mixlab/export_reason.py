"""Export a concert-mixlab preset + excerpt as a Reason 14 song (.reason, RS1300).

Layout written (phase 1):
  * one audio track per input group: "Close L" (mono), "Close R" (mono), "Camera" (stereo)
  * per-layer delay = clip start offset in PPQ (sub-ms resolution; 120 BPM -> 1 PPQ = 0.0326 ms)
  * channel strip: Invert Phase, plus any raw byte/long params supplied by a mapper (gain, pan, EQ ...)
  * master insert: MClass Maximizer (when the preset's limiter is on)

Templates (all saved by Reason 14, see reason_templates/):
  audio_track_eq.reason   HW + Master + one Disk Channel (audio track), EQ switched on
  maximizer.reason        Master with an MClass Maximizer insert (DCHL + 4 cables)
  stereo_imager.reason    Disk Channel with an MClass Stereo Imager insert (phase 2: bass shuffle)
  audio_recording_templates.iff   TRRF/TNMF/SASF from a song with one imported audio clip

Requires the reason_spec library (REASON_SPEC env or ~/GitHubProjects/reason_spec).
"""
from __future__ import annotations

import copy
import os
import struct
import sys
from array import array
from pathlib import Path

SPEC = Path(os.environ.get("REASON_SPEC", Path.home() / "GitHubProjects/reason_spec"))
sys.path.insert(0, str(SPEC))
import reasonspec  # noqa: E402
from reasonspec import audio as AU, prim, sequencer as S  # noqa: E402
from reasonspec.chunks import Chunk, Group, parse_list  # noqa: E402
from reasonspec.notes import Clip, new_snic  # noqa: E402

TEMPL = Path(__file__).resolve().parent.parent / "reason_templates"
PPQ_PER_QUARTER = 15360
BAR = 4 * PPQ_PER_QUARTER
DISK = "X15 Main Mixer Disk Channel"
MASTER_ID = 0xFEE

# Channel strip byte-param indices (EBParameters order, ChannelDeviceProtocol.h)
BP_WIDTH, BP_MUTE, BP_SOLO, BP_INPUT_GAIN, BP_INVERT, BP_EQ_ON = 0, 1, 2, 3, 4, 10


# --- small chunk helpers ---------------------------------------------------------------------------

def leaf(g: Group, key: bytes) -> Chunk:
    n = g.find(key)
    assert isinstance(n, Chunk), key
    return n


def group(g: Group, key: bytes) -> Group:
    n = g.find(key)
    assert isinstance(n, Group), key
    return n


def find_deep(root: Group, kind: bytes) -> Group:
    return next(n for _, n in root.walk() if isinstance(n, Group) and n.kind == kind)


def u32(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


# --- PARM (device parameters) ---------------------------------------------------------------------

def parm_decode(data: bytes):
    ver, o = data[:5], 5
    n = data[o]; o += 1
    bp = {}
    for _ in range(n):
        bp[data[o]] = data[o + 1]; o += 2
    m = data[o]; o += 1
    lp = {}
    for _ in range(m):
        lp[data[o]] = u32(data, o + 1); o += 5
    assert o == len(data), (o, len(data))
    return ver, bp, lp


def parm_encode(ver: bytes, bp: dict, lp: dict) -> bytes:
    out = bytearray(ver) + bytes([len(bp)])
    for k in sorted(bp):
        out += bytes([k, bp[k] & 0xFF])          # signed params are stored as two's complement u8
    out += bytes([len(lp)])
    for k in sorted(lp):
        out += bytes([k]) + struct.pack(">I", lp[k] & 0xFFFFFFFF)
    return bytes(out)


def set_params(devi: Group, bp: dict | None = None, lp: dict | None = None) -> None:
    p = leaf(devi, b"PARM")
    ver, b0, l0 = parm_decode(p.data)
    for k, v in (bp or {}).items():
        assert k in b0, f"byte param {k} not saved in this device"
        b0[k] = v
    for k, v in (lp or {}).items():
        assert k in l0, f"long param {k} not saved in this device"
        l0[k] = v
    p.data = parm_encode(ver, b0, l0)


# --- device identity ------------------------------------------------------------------------------

def devi_id(devi: Group) -> int:
    return u32(leaf(devi, b"DESC").data, 6)


def devi_strings(desc: bytes):
    """(offset, string) of the display name: first length-prefixed printable string after the id."""
    o = 10
    while o + 4 <= len(desc):
        n = u32(desc, o)
        if 0 < n < 64 and o + 4 + n <= len(desc) and all(32 <= c < 127 for c in desc[o + 4:o + 4 + n]):
            return o, desc[o + 4:o + 4 + n].decode()
        o += 1
    raise ValueError("no name in DESC")


def set_identity(devi: Group, new_id: int, name: str | None = None) -> None:
    d = leaf(devi, b"DESC")
    b = bytearray(d.data); b[6:10] = struct.pack(">I", new_id)
    if name is not None:
        o, old = devi_strings(bytes(b))
        b[o:o + 4 + len(old)] = prim.string(name)
    d.data = bytes(b)


def cnct(in_dev, in_sock, out_dev, out_sock, auto=1, template: Chunk | None = None) -> Chunk:
    ver = template.data[:5] if template else prim.version(2, 1)
    return Chunk(b"CNCT", ver + bytes([0]) + struct.pack(">IIIII", in_dev, in_sock, out_dev, out_sock, auto))


# --- the exporter ---------------------------------------------------------------------------------

def load_templates():
    c, doc = reasonspec.load(TEMPL / "audio_track_eq.reason")
    _, mdoc = reasonspec.load(TEMPL / "maximizer.reason")
    trrf, tnmf, sasf = parse_list((TEMPL / "audio_recording_templates.iff").read_bytes())
    return c, doc, mdoc, trrf, tnmf, sasf


def add_recording_clip(track, trrf_t, tnmf_t, sasf_t, rec_id, sid, samples, channels, rate, bpm,
                       start_ppq, name, volume=AU.UNITY_VOLUME):
    """Put `samples` (interleaved float) on `track` as recording `rec_id` + one clip at start_ppq."""
    frames = len(samples) // channels
    length = AU.recording_length_ppq(frames, rate, bpm)
    data, ru, su = AU.make_stream(samples, channels, rate, 0)

    if S._group(track.node, b"TRRF") is None:
        kids = track.node.children
        i = next(j for j, n in enumerate(kids) if isinstance(n, Chunk) and n.id == b"TRSC")
        trrf = copy.deepcopy(trrf_t)
        trrl = group(trrf, b"TRRL")
        trrl.children[:] = [n for n in trrl.children if not (isinstance(n, Group) and n.kind == b"TREF")]
        kids[i + 1:i + 1] = [trrf, copy.deepcopy(tnmf_t)]
    trrl = group(group(track.node, b"TRRF"), b"TRRL")
    tref = copy.deepcopy(next(n for n in group(trrf_t, b"TRRL").children if isinstance(n, Group) and n.kind == b"TREF"))
    rec = AU.Recording(tref)
    rec.set_stream(sid, rate, channels, ru, su)
    rec.set_tempo(bpm)
    trri = rec.trri
    name_end = rec._fields()[10]
    rest = prim.u32(61) + trri.data[name_end + 4:]                                   # createMode Imported
    trri.data = (trri.data[:5] + prim.u32(rec_id) + trri.data[9:21] + prim.s32(0) + trri.data[25:29]
                 + prim.string(name) + rest)
    trtd = S._leaf(S._group(tref, b"TRRT"), b"TRTD")
    trtd.data = trtd.data[:5] + prim.s32(0) + trtd.data[9:]
    trrl.children.append(tref)

    snif = Group(b"FORM", b"SNIF", [Chunk(b"SNIC", new_snic(start_ppq, length, name)), copy.deepcopy(sasf_t)])
    ac = AU.AudioClip(Clip(snif))
    row = ac.rows[0]
    row.arit.data = row.arit.data[:5] + prim.u32(rec_id) + row.arit.data[9:]
    row.clear_slices()
    row.set_recording_offset(0)
    sasc = bytearray(ac.sasc.data); sasc[9:11] = prim.u16(0); ac.sasc.data = bytes(sasc)  # stretch off
    ac.set_fades(0, 0)
    ac.set_volume(volume)
    AU.audio_lanes(track)[0].arrangement(0, create=True).children.append(snif)
    return data, start_ppq + length


def export(tracks_spec, out_path, *, bpm=120_000, rate=48_000, maximizer=None, buses=None):
    """tracks_spec: list of dict(name, samples (interleaved float32 array), channels, delay_ms,
    bp={idx: raw}, lp={idx: raw}, clip_volume=734). maximizer: None or dict(bp=..., lp=...)."""
    c, doc, mdoc, trrf_t, tnmf_t, sasf_t = load_templates()
    rs = doc.rs10
    assert struct.unpack_from(">I", S._leaf(S.sequencer(doc), b"TRDC").data, 23)[0] == bpm, "template tempo"
    devl = group(rs, b"DEVL")
    disk0 = next(d for d in devl.children if devi_id(d) == 0xFEF)
    track0 = next(t for t in S.tracks(doc) if t.dest_device_id == 0xFEF)
    tfct = find_deep(rs, b"TFCT")
    dinh = next(n for n in doc.body if isinstance(n, Group) and n.kind == b"DINH")
    stil = group(dinh, b"STIL")
    stin_audio = [n for n in stil.children if isinstance(n, Group) and n.kind == b"STIN"][-1]

    ids = [0xFEF] + list(range(1, len(tracks_spec)))
    devis, trks = [disk0], [track0.node]
    for k in range(1, len(tracks_spec)):
        d = copy.deepcopy(disk0); devl.children.append(d); devis.append(d)
        t = copy.deepcopy(track0.node)
        trdc = leaf(t, b"TRDC"); trdc.data = trdc.data[:9] + struct.pack(">I", ids[k]) + trdc.data[13:]
        tfct.children.append(t); trks.append(t)
        stil.children.append(copy.deepcopy(stin_audio))
    for d, i, ts in zip(devis, ids, tracks_spec):
        set_identity(d, i, ts["name"])
        set_params(d, ts.get("bp"), ts.get("lp"))

    # mixer plan / channel routing / rack (one rack per track)
    buses = buses or []
    next_id = max(ids[1:] + [0]) + 1
    bus_of = {}                                                      # track index -> bus id
    bus_t = parse_list((TEMPL / "mix_channel_bus.iff").read_bytes())[0]
    _, idoc = reasonspec.load(TEMPL / "stereo_imager.reason")
    imager_t = next(d for d in group(idoc.rs10, b"DEVL").children if leaf(d, b"DESC").data.find(b"StereoImager") > 0)
    cctl = group(rs, b"CCTL"); ctmpl = cctl.children[0]
    bus_ids = []
    for bspec in buses:
        bid = next_id; next_id += 1; bus_ids.append(bid)
        bd = copy.deepcopy(bus_t); set_identity(bd, bid, bspec["name"]); set_params(bd, bspec.get("bp"), bspec.get("lp"))
        devl.children.append(bd)
        for k in bspec["sources"]:
            bus_of[k] = bid
        if bspec.get("imager"):
            im = copy.deepcopy(imager_t); iid = next_id; next_id += 1
            set_identity(im, iid)
            pp = leaf(group(im, b"PTCH"), b"PARM"); ver, b0, l0 = parm_decode(pp.data)
            for k, v in bspec["imager"]["patch_bp"].items():
                assert k in b0, k
                b0[k] = v
            pp.data = parm_encode(ver, b0, l0)
            devl.children.append(im)
            kids = bd.children
            kids.insert(next(j for j, n in enumerate(kids) if isinstance(n, Chunk) and n.id == b"PARM"),
                        Chunk(b"DCHL", prim.version(1, 0) + struct.pack(">II", 1, iid)))
            # channel kAOToInsertFX L/R (0/1) -> imager in 0/1 ; imager out 0/1 -> channel kAIFromInsertFX L/R (0/1)
            cctl.children += [cnct(iid, 0, bid, 0, template=ctmpl), cnct(iid, 1, bid, 1, template=ctmpl),
                              cnct(bid, 0, iid, 0, template=ctmpl), cnct(bid, 1, iid, 1, template=ctmpl)]
            bspec["imager_id"] = iid
    order = []
    for k, i in enumerate(ids):
        order.append(i)
        for bid, bspec in zip(bus_ids, buses):
            if k == max(bspec["sources"]):
                order.append(bid)
    plan = leaf(rs, b"PLAN"); plan.data = plan.data[:5] + struct.pack(">I", len(order)) + b"".join(struct.pack(">I", i) for i in order)
    plcl = group(rs, b"PLCL"); pt = plcl.children[0]
    plcl.children[:] = [Chunk(b"PLNC", pt.data[:5] + struct.pack(">II", i, bus_of.get(k, MASTER_ID))) for k, i in enumerate(ids)] + \
                       [Chunk(b"PLNC", pt.data[:5] + struct.pack(">II", b, MASTER_ID)) for b in bus_ids]
    rack = leaf(rs, b"RACK"); r = rack.data
    racks = [[0, MASTER_ID]] + [[i] for i in order]
    rack.data = r[:5] + struct.pack(">I", len(racks)) + b"".join(struct.pack(">I", len(x)) + b"".join(struct.pack(">I", i) for i in x) for x in racks) + r[-2:]
    # track counts
    tfdc = leaf(find_deep(rs, b"TFFM"), b"TFDC")
    # TFDC: version(5) isOpen(1) mute(1) solo(1) color(?) then child count; locate the old count and bump it
    n_tracks = len([n for n in tfct.children if isinstance(n, Group) and n.kind == b"TRFM"])
    old = n_tracks - (len(ids) - 1)
    b = bytearray(tfdc.data); pos = b.find(struct.pack(">I", old), 5)
    assert pos > 0, tfdc.data.hex(); b[pos:pos + 4] = struct.pack(">I", n_tracks); tfdc.data = bytes(b)
    sq_trdc = S._leaf(S.sequencer(doc), b"TRDC")
    sq_trdc.data = sq_trdc.data[:5] + struct.pack(">I", n_tracks) + sq_trdc.data[9:]

    # audio
    streams, end, taken = {}, 0, set()
    for t_node, ts in zip(trks, tracks_spec):
        track = S.Track(t_node, 0)
        sid = reasonspec.free_stream_id(c, taken); taken.add(sid)
        # Sample-exact delay: clip start on a 16-PPQ grid (16 PPQ = exactly 25 samples at 120 BPM / 48 kHz)
        # plus 0..24 leading zero samples inside the recording.
        assert bpm == 120_000 and rate == 48_000, "sample-exact delay grid assumes 120 BPM / 48 kHz"
        d = round(ts.get("delay_ms", 0.0) * rate / 1000)
        start, pad = 16 * (d // 25), d % 25
        ts["start_ppq"], ts["pad_samples"], ts["delay_samples"] = start, pad, d
        smp = ts["samples"]
        if pad:
            smp = array("f", bytes(4 * pad * ts["channels"])) + smp
        data, e = add_recording_clip(track, trrf_t, tnmf_t, sasf_t, 0, sid, smp, ts["channels"], rate, bpm,
                                     start, ts["name"], ts.get("clip_volume", AU.UNITY_VOLUME))
        streams[sid] = data; end = max(end, e)
    S.ensure_song_end(doc, (end // BAR + 2) * BAR)

    # master insert: MClass Maximizer
    if maximizer is not None:
        mrs = mdoc.rs10
        mx = copy.deepcopy(next(d for d in group(mrs, b"DEVL").children if leaf(d, b"DESC").data.find(b"MClass Maximizer") > 0))
        mx_id = next_id; next_id += 1
        set_identity(mx, mx_id)
        set_params(mx, maximizer.get("bp"), maximizer.get("lp"))
        if maximizer.get("patch_bp"):
            pp = leaf(group(mx, b"PTCH"), b"PARM"); ver, b0, l0 = parm_decode(pp.data)
            for k, v in maximizer["patch_bp"].items():
                assert k in b0, k
                b0[k] = v
            pp.data = parm_encode(ver, b0, l0)
        devl.children.append(mx)
        master = next(d for d in devl.children if devi_id(d) == MASTER_ID)
        mmaster = next(d for d in group(mrs, b"DEVL").children if devi_id(d) == MASTER_ID)
        dchl = copy.deepcopy(leaf(mmaster, b"DCHL")); dchl.data = dchl.data[:-4] + struct.pack(">I", mx_id)
        kids = master.children
        assert master.find(b"DCHL") is None
        kids.insert(next(j for j, n in enumerate(kids) if isinstance(n, Chunk) and n.id == b"PARM"), dchl)
        tmpl = ctmpl
        cctl.children += [cnct(MASTER_ID, 16, mx_id, 0, template=tmpl), cnct(MASTER_ID, 17, mx_id, 1, template=tmpl),
                          cnct(mx_id, 0, MASTER_ID, 16, template=tmpl), cnct(mx_id, 1, MASTER_ID, 17, template=tmpl)]

    reasonspec.save(out_path, c, doc, streams)
    # read back
    c2, d2 = reasonspec.load(out_path)
    for t in S.tracks(d2):
        for rcd in AU.recordings(t):
            h = AU.read_stream_header(c2.read_stream(rcd.stream_id))
            assert h.stream_uuid == rcd.stream_uuid and h.sample_rate == rcd.frame_rate
    return dict(path=str(out_path), tracks=[(ts["name"], ts["start_ppq"]) for ts in tracks_spec], streams=sorted(streams))


# --- concert-mixlab preset -> tracks ---------------------------------------------------------------

def clip_volume_for_db(db: float) -> tuple[int, float]:
    """Audio-clip gain: gain = (v/734)^3, v in 0..1000 (max +8.06 dB). Returns (v, dB left over)."""
    v = round(734 * 10 ** (db / 60))
    vc = max(0, min(1000, v))
    return vc, db - (60 * __import__("math").log10(vc / 734) if vc else -200)


def preset_tracks(preset: dict, src_dir: str, start: float, dur: float, read_audio):
    """One Reason audio track per mixlab input group: a stereo file whose L/R are hard-panned with equal
    gain/polarity becomes one stereo track, every other input a mono track. Returns (tracks_spec, notes)."""
    tracks, notes = [], []
    for L in preset["layers"]:
        if L.get("mute"):
            notes.append(f"layer {L['name']} muted: not exported"); continue
        byfile = {}
        for i in L["inputs"]:
            byfile.setdefault(i["file"], []).append(i)
        for fn, ins in byfile.items():
            x = read_audio(os.path.join(src_dir, fn), start, dur)
            chs = {i.get("ch", 0): i for i in ins}
            stereo = (set(chs) == {0, 1} and chs[0].get("pan", 0) == -1 and chs[1].get("pan", 0) == 1
                      and chs[0].get("gain_db", 0) == chs[1].get("gain_db", 0)
                      and bool(chs[0].get("invert")) == bool(chs[1].get("invert")))
            groups = [(ins, 2)] if stereo else [([i], 1) for i in ins]
            for g, nch in groups:
                i0 = g[0]
                if nch == 2:
                    a = x[:, :2].astype("float32").reshape(-1)
                    name = L["name"].capitalize()
                else:
                    a = x[:, i0.get("ch", 0)].astype("float32").copy()
                    name = f"{L['name'].capitalize()} {'L' if i0.get('pan', 0) < 0 else 'R' if i0.get('pan', 0) > 0 else i0.get('ch', 0) + 1}"
                gain_db = L.get("gain_db", 0.0) + i0.get("gain_db", 0.0)
                vol, rest = clip_volume_for_db(gain_db)
                inv = bool(L.get("invert")) ^ bool(i0.get("invert"))
                t = dict(name=name, samples=array("f", a.tobytes()), channels=nch, delay_ms=L.get("delay_ms", 0.0),
                         clip_volume=vol, bp={BP_INVERT: 1} if inv else {}, lp={},
                         layer=L["name"], pan=i0.get("pan", 0.0) if nch == 1 else None, gain_db=gain_db,
                         width=L.get("width", 1.0), filters=L.get("filters", []), side_filters=L.get("side_filters", []))
                if abs(rest) > 0.05:
                    notes.append(f"{name}: {gain_db:+.2f} dB exceeds clip-gain range, {rest:+.2f} dB not applied yet")
                tracks.append(t)
    return tracks, notes


# --- mixlab settings -> Reason device knobs --------------------------------------------------------

def _mixlab_routing(t):
    """mixlab (gL, gR) for a mono track after input pan (constant power) and layer width (M/S); linear,
    excluding gain. Stereo tracks: None (handled by the channel Width knob)."""
    import math
    if t["channels"] != 1:
        return None
    th = (t["pan"] + 1) * math.pi / 4
    gl, gr = math.cos(th), math.sin(th)
    w = t["width"]
    return (1 + w) / 2 * gl + (1 - w) / 2 * gr, (1 - w) / 2 * gl + (1 + w) / 2 * gr


def fit_imager(side_sos, sr: float = 48000.0):
    """Choose MClass Stereo Imager (low width, crossover) so that side/mid of the imager ≈ the mixlab side EQ.
    Returns (low_width_raw, xover_raw, H_mid(freqs), freqs, ratio_rms_db)."""
    import numpy as np
    from scipy import signal
    import reason_devices as RD
    freqs = np.geomspace(20, 20000, 240)
    _, hs = signal.sosfreqz(side_sos, worN=freqs, fs=sr)
    tgt = 20 * np.log10(np.abs(hs))
    w = np.where(freqs < 2000, 1.0, 0.3)
    z = np.exp(-2j * np.pi * freqs / sr)
    best = None
    for xr in range(0, 128):
        a, b, g = RD.imager_lpf_coeffs(xr, sr)
        lp = float(g) / (1 - float(a) * z + float(b) * z * z)
        for hr in (range(-12, 13) if os.environ.get("MIXLAB_IMAGER_HIGH0") is None else (0,)):
            hgm, hgs = RD.imager_mid_side_gains(hr)
            for wr in range(-64, 64):
                gm, gs = RD.imager_mid_side_gains(wr)
                hm = hgm + (gm - hgm) * lp; hsd = hgs + (gs - hgs) * lp
                err = np.sqrt(np.mean(((20 * np.log10(np.abs(hsd / hm)) - tgt) * w) ** 2))
                if best is None or err < best[0]:
                    best = (err, wr, xr, hm, hr)
    err, wr, xr, hm, hr = best
    return wr, xr, hm, freqs, float(err), hr


def map_to_reason(preset: dict, tracks: list, sr: float = 48000.0):
    """Fill each track's channel PARM (input gain, Ø, pan, width, fader, EQ fit) and the Maximizer.
    Gains: per-track total = layer + input gain (+ master gain when no limiter) + pan-law compensation.
    Coarse part on Input Gain (0.286 dB steps), remainder on the fader (≈0.035 dB steps); clip gain 0 dB."""
    import math
    import numpy as np
    from scipy import signal
    import reason_devices as RD
    import mixlab as ML
    notes, fits = [], {}
    m = preset.get("master", {})
    lim_on = m.get("limiter_ceiling") is not None
    master_db = m.get("gain_db", 0.0)
    mx = None
    if lim_on:
        # Below threshold the Maximizer's gain is in·out·0.99 (output gain is applied even when not limiting),
        # and it limits at 0.93·out·0.99 (look-ahead). So: out from the ceiling, in = master − out (dB).
        ceil = float(m["limiter_ceiling"])
        out_raw = min(range(128), key=lambda r: abs(0.93 * RD.maximizer_output_gain_linear(r) - ceil))
        out_db_eff = 20 * math.log10(RD.maximizer_output_gain_linear(out_raw))
        in_raw = RD.maximizer_gain_db_to_raw(master_db - out_db_eff)
        master_rest = master_db - out_db_eff - 20 * math.log10(RD.maximizer_input_gain_linear(in_raw))
        att = min(RD.MAXIMIZER_ATTACK_MS, key=lambda k: abs(RD.MAXIMIZER_ATTACK_MS[k] - m.get("attack_ms", 5)))
        rel = min((0, 1), key=lambda k: abs(RD.MAXIMIZER_RELEASE_MS[k] - m.get("release_ms", 50)))
        pb = RD.MAXIMIZER_BP
        mx = dict(patch_bp={pb["Input Gain"]: in_raw, pb["Limiter Enable"]: 1, pb["Look Ahead Enable"]: 1,
                            pb["Attack Speed"]: att, pb["Release Speed"]: rel, pb["Output Gain"]: out_raw,
                            pb["Soft Clip Enable"]: 0},
                  summary=dict(input_gain_db=RD.maximizer_gain_db(in_raw), output_gain_db=RD.maximizer_gain_db(out_raw),
                               ceiling_dbfs=20 * math.log10(0.93 * RD.maximizer_output_gain_linear(out_raw)),
                               attack_ms=RD.MAXIMIZER_ATTACK_MS[att], release_ms=RD.MAXIMIZER_RELEASE_MS[rel], look_ahead=True))
        notes.append(f"Maximizer: look-ahead limiter, attack {RD.MAXIMIZER_ATTACK_MS[att]} ms / release "
                     f"{RD.MAXIMIZER_RELEASE_MS[rel]} ms (mixlab {m.get('attack_ms', 5)}/{m.get('release_ms', 50)} ms); "
                     "L/R are limited independently in Reason")
    else:
        master_rest = master_db
    freqs = np.geomspace(20, 20000, 240)
    buses, bus_layer = [], {}
    for L in preset["layers"]:
        ssos = ML.layer_sos(L, "side_filters")
        k = [j for j, t in enumerate(tracks) if t["layer"] == L["name"]]
        if ssos is None or not k:
            continue
        wr, xr, hm, fq, ierr, hr = fit_imager(ssos, sr)
        sos = ML.layer_sos(L)
        _, h = signal.sosfreqz(sos, worN=freqs, fs=sr) if sos is not None else (None, np.ones_like(freqs))
        tgt = 20 * np.log10(np.abs(h) + 1e-12) - 20 * np.log10(np.abs(hm))     # bus EQ also undoes the imager's mid change
        fit = RD.fit_channel_eq(tgt, freqs, sr, weights=np.where(freqs < 30, 0.3, 1.0))
        bp_, lp_ = RD.channel_params_to_parm(fit["params"])
        ib = RD.IMAGER_BP
        buses.append(dict(name=f"{L['name'].capitalize()} Bus", sources=k, bp=bp_, lp=lp_, layer=L["name"], params=fit["params"],
                          imager=dict(patch_bp={ib["Low Width"]: wr, ib["High Width"]: hr, ib["X-Over Frequency"]: xr}),
                          summary=dict(imager_low_width=wr, imager_high_width=hr, imager_low_factor=RD.imager_width_factor(wr),
                                       imager_xover_hz=round(RD.imager_xover_hz(xr), 1), side_vs_mid_fit_rms_db=round(ierr, 2),
                                       eq_fit_rms_db=round(fit["rms_db"], 2), eq_fit_max_db=round(fit["max_abs_db"], 2))))
        bus_layer[L["name"]] = buses[-1]
        notes.append(f"{L['name']}: side EQ → bus '{buses[-1]['name']}' with MClass Stereo Imager (low width {wr}, "
                     f"x-over {RD.imager_xover_hz(xr):.0f} Hz) + bus EQ that restores the mid; side/mid fit {ierr:.2f} dB rms")
    for t in tracks:
        p = {}
        layer = next(L for L in preset["layers"] if L["name"] == t["layer"])
        # EQ: fit the layer's RBJ chain to the channel EQ (once per layer); on the bus when the layer has one
        if t["layer"] in bus_layer:
            fits[t["layer"]] = None
        elif t["layer"] not in fits:
            sos = ML.layer_sos(layer)
            if sos is None:
                fits[t["layer"]] = None
            else:
                _, h = signal.sosfreqz(sos, worN=freqs, fs=sr)
                tgt = 20 * np.log10(np.abs(h) + 1e-12)
                wts = np.where(freqs < 30, 0.3, 1.0)
                fits[t["layer"]] = RD.fit_channel_eq(tgt, freqs, sr, weights=wts)
        fit = fits[t["layer"]]
        if fit is not None:
            p.update(fit["params"])
        # pan / width and level compensation
        if t["channels"] == 1:
            gl, gr = _mixlab_routing(t)
            ratio = math.atan2(gr, gl)
            pan_raw = min(range(-100, 101), key=lambda r: abs(math.atan2(RD.pan_gains(r)[1], RD.pan_gains(r)[0]) - ratio))
            lp_, rp_ = RD.pan_gains(pan_raw)
            comp_db = 20 * math.log10(math.hypot(gl, gr) / math.hypot(lp_, rp_))
            p.update({"Pan": pan_raw})
        else:
            p.update({"Pan": 0, "Width": RD.width_to_raw(min(1.0, t["width"]))})
            comp_db = 0.0
            if t["width"] > 1:
                notes.append(f"{t['name']}: width {t['width']} > 1 not possible on a channel (max 100%)")
        total = t["gain_db"] + master_rest + comp_db
        g_raw = max(-64, min(63, round(total / (18 / 63))))
        rest = total - 20 * math.log10(RD.input_gain_linear(g_raw))
        f_raw = RD.fader_db_to_raw(rest)
        p.update({"Input Gain": g_raw, "Level": f_raw, "Invert Phase": 1 if t["bp"].get(BP_INVERT) else 0})
        if abs(RD.fader_db(f_raw) - rest) > 0.05:
            notes.append(f"{t['name']}: gain {total:+.2f} dB out of range")
        bp, lp = RD.channel_params_to_parm(p)
        t["bp"], t["lp"], t["clip_volume"] = bp, lp, AU.UNITY_VOLUME
        t["reason"] = dict(params=p, input_gain_db=RD.input_gain_db(g_raw), fader_db=RD.fader_db(f_raw),
                           pan=pan_raw if t["channels"] == 1 else 0, eq_fit_rms_db=None if fit is None else fit["rms_db"],
                           eq_fit_max_db=None if fit is None else fit["max_abs_db"])
        if t.get("side_filters") and t["layer"] not in bus_layer:
            notes.append(f"{t['name']}: side EQ not exported")
    return mx, notes, fits, buses


def model_render(tracks: list, mx: dict | None, sr: float = 48000.0, with_maximizer=False, buses=None):
    """Render the exported song with the reason_devices model (channel strips, buses + Imager, optional Maximizer)."""
    import numpy as np
    import reason_devices as RD
    buses = buses or []
    n = max(len(t["samples"]) // t["channels"] + t["delay_samples"] for t in tracks)
    out = np.zeros((n, 2))
    bus_in = [np.zeros((n, 2)) for _ in buses]
    dest = {k: bi for bi, b in enumerate(buses) for k in b["sources"]}
    for k, t in enumerate(tracks):
        x = np.frombuffer(t["samples"].tobytes(), dtype=np.float32).astype(float)
        if t["channels"] == 2:
            x = x.reshape(-1, 2)
        y = RD.channel_process(x, sr, t["reason"]["params"])
        d = t["delay_samples"]                                               # clip start (16-PPQ grid) + zero pad
        (bus_in[dest[k]] if k in dest else out)[d:d + len(y)] += y
    for b, xb in zip(buses, bus_in):
        q = b["imager"]["patch_bp"]; ib = RD.IMAGER_BP
        ins = lambda y, q=q: RD.imager_process(y, sr, q[ib["Low Width"]], q[ib["High Width"]], q[ib["X-Over Frequency"]])
        out += RD.channel_process(xb, sr, b["params"], insert=ins)
    if mx is not None:
        pb = RD.MAXIMIZER_BP; q = mx["patch_bp"]
        g = RD.maximizer_input_gain_linear(q[pb["Input Gain"]]) * RD.maximizer_output_gain_linear(q[pb["Output Gain"]])
        if with_maximizer:
            out = RD.maximizer_process(out, sr, q[pb["Input Gain"]], 1, 1, q[pb["Attack Speed"]], q[pb["Release Speed"]],
                                       q[pb["Output Gain"]], 0, 64)
        else:
            out = out * g        # Maximizer gains without limiting (Reason compensates the look-ahead delay)
    return out



if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--selftest":   # 3 tracks of test tones
    import math
    sr, n = 48_000, 48_000 * 4
    def tone(f, ch):
        a = array("f", [0.0]) * (n * ch)
        for i in range(n):
            v = 0.2 * math.sin(2 * math.pi * f * i / sr)
            for c_ in range(ch):
                a[i * ch + c_] = v
        return a
    spec = [dict(name="Close L", samples=tone(220, 1), channels=1, delay_ms=11.25),
            dict(name="Close R", samples=tone(330, 1), channels=1, delay_ms=11.25),
            dict(name="Camera", samples=tone(440, 2), channels=2, delay_ms=0.0, bp={BP_INVERT: 1})]
    print(export(spec, Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/mixlab_export_test.reason"), maximizer={}))
