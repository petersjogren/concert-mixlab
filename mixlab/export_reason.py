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


def export(tracks_spec, out_path, *, bpm=120_000, rate=48_000, maximizer=None):
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
    plan = leaf(rs, b"PLAN"); plan.data = plan.data[:5] + struct.pack(">I", len(ids)) + b"".join(struct.pack(">I", i) for i in ids)
    plcl = group(rs, b"PLCL"); pt = plcl.children[0]
    plcl.children[:] = [Chunk(b"PLNC", pt.data[:5] + struct.pack(">II", i, MASTER_ID)) for i in ids]
    rack = leaf(rs, b"RACK"); r = rack.data
    racks = [[0, MASTER_ID]] + [[i] for i in ids]
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
        start = round(ts.get("delay_ms", 0.0) / 1000 * bpm / 1000 / 60 * PPQ_PER_QUARTER)
        ts["start_ppq"] = start
        data, e = add_recording_clip(track, trrf_t, tnmf_t, sasf_t, 0, sid, ts["samples"], ts["channels"], rate, bpm,
                                     start, ts["name"], ts.get("clip_volume", AU.UNITY_VOLUME))
        streams[sid] = data; end = max(end, e)
    S.ensure_song_end(doc, (end // BAR + 2) * BAR)

    # master insert: MClass Maximizer
    if maximizer is not None:
        mrs = mdoc.rs10
        mx = copy.deepcopy(next(d for d in group(mrs, b"DEVL").children if leaf(d, b"DESC").data.find(b"MClass Maximizer") > 0))
        mx_id = max(ids[1:] + [0]) + 1
        set_identity(mx, mx_id)
        set_params(mx, maximizer.get("bp"), maximizer.get("lp"))
        devl.children.append(mx)
        master = next(d for d in devl.children if devi_id(d) == MASTER_ID)
        mmaster = next(d for d in group(mrs, b"DEVL").children if devi_id(d) == MASTER_ID)
        dchl = copy.deepcopy(leaf(mmaster, b"DCHL")); dchl.data = dchl.data[:-4] + struct.pack(">I", mx_id)
        kids = master.children
        assert master.find(b"DCHL") is None
        kids.insert(next(j for j, n in enumerate(kids) if isinstance(n, Chunk) and n.id == b"PARM"), dchl)
        cctl = group(rs, b"CCTL"); tmpl = cctl.children[0]
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
