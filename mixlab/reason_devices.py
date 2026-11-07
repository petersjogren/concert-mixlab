"""Reason device parameter maps and DSP ports (derived from the Reason C++ source).

Three devices:

* Main mixer channel strip ("X15 Main Mixer Disk Channel" / "X15 Main Mixer Mix Channel")
  - Src/Devices/MainMixer/ChannelDevice/ChannelDeviceProtocol.{h,cpp}  (parameter tables, maps, coeffs)
  - Src/Devices/MainMixer/ChannelDevice/DLLChannelEQDSP.cpp            (filters + EQ render)
  - Src/Devices/MainMixer/ChannelDevice/DLLChannelDevice.cpp           (signal path, pan/width/fader)
  - Src/Foundation/Audio/AudioVolumeGain.h                             (fader law)
* "MClass Stereo StereoImager"  - Src/Devices/MClass/MStereoImager/
* "MClass Maximizer"            - Src/Devices/MClass/MMaximizer/

All functions are pure (no I/O).  Coefficients are computed in float32 to mirror the C++
`float` arithmetic; filtering itself runs in float64 (scipy.signal.lfilter), so results
differ from Reason by float32 rounding noise only (in theory).  NOTHING here has been
verified against Reason-rendered audio yet -- see docs/reason-devices.md.

Line citations below refer to the files above, paths relative to Reason/Src.
"""

from __future__ import annotations

import math
import struct
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy import signal as _sig

F32 = np.float32
KBATCH = 64  # Foundation/Base/ReAlizerConstants.h:16

# ---------------------------------------------------------------------------
# PARM chunk value encoding
# ---------------------------------------------------------------------------
# PersistantPropertyValue.cpp:47-55 / 99-107: byte param = u8 id + u8 value, long param =
# u8 id + Write32Bit(value).  TByteParameterValue is std::int8_t and TLongParameterValue is
# std::int32_t (Core/Core/Jukebox/PropertyPackage/PropertyPackage.h:63-64), so negative
# values are stored as plain two's complement (e.g. -64 -> 0xC0, pan -100 -> 0xFFFFFF9C).
# The value is NOT offset-encoded.  (Contrast: the Maximizer gains are *defined* 0..127
# with 64 = 0 dB, so they are never negative.)


def encode_byte_value(v: int) -> int:
    """Signed byte parameter value (-128..127) -> stored u8 (two's complement)."""
    v = int(v)
    if not -128 <= v <= 127:
        raise ValueError(f"byte parameter out of int8 range: {v}")
    return v & 0xFF


def decode_byte_value(u: int) -> int:
    """Stored u8 -> signed byte parameter value (int8)."""
    u = int(u) & 0xFF
    return u - 256 if u >= 128 else u


def encode_long_value(v: int) -> int:
    """Signed long parameter value -> stored u32 (two's complement, written big-endian)."""
    v = int(v)
    if not -(2 ** 31) <= v < 2 ** 31:
        raise ValueError(f"long parameter out of int32 range: {v}")
    return v & 0xFFFFFFFF


def decode_long_value(u: int) -> int:
    u = int(u) & 0xFFFFFFFF
    return u - 2 ** 32 if u >= 2 ** 31 else u


def parse_parm(payload: bytes) -> Tuple[bytes, Dict[int, int], Dict[int, int]]:
    """Parse a PARM chunk payload: 5-byte version, u8 nBP, (u8 id, i8 val)*, u8 nLP, (u8 id, i32 BE)*.

    Returns (version_bytes, {bp_id: signed value}, {lp_id: signed value}).
    Layout: PropertyUtils.cpp:480-500 (InternalWriteParametersOfType).
    """
    ver = bytes(payload[:5])
    p = 5
    nb = payload[p]; p += 1
    bp: Dict[int, int] = {}
    for _ in range(nb):
        bp[payload[p]] = decode_byte_value(payload[p + 1]); p += 2
    nl = payload[p]; p += 1
    lp: Dict[int, int] = {}
    for _ in range(nl):
        (u,) = struct.unpack(">I", bytes(payload[p + 1:p + 5]))
        lp[payload[p]] = decode_long_value(u); p += 5
    return ver, bp, lp


def build_parm(version: bytes, bp: Mapping[int, int], lp: Mapping[int, int]) -> bytes:
    """Inverse of parse_parm.  Parameters are written in ascending id order, like Reason."""
    out = bytearray(version)
    out.append(len(bp))
    for k in sorted(bp):
        out += bytes([k, encode_byte_value(bp[k])])
    out.append(len(lp))
    for k in sorted(lp):
        out += bytes([k]) + struct.pack(">I", encode_long_value(lp[k]))
    return bytes(out)


# ---------------------------------------------------------------------------
# Channel strip parameter tables
# ---------------------------------------------------------------------------
# (name, default, min, max, savedInSong)  -- ChannelDeviceProtocol.cpp:34-234 (gBPInfo),
# index = position in enum EBParameters (ChannelDeviceProtocol.h).
_BP_TABLE: List[Tuple[str, int, int, int, bool]] = [
    ("Width", 127, 0, 127, True),
    ("Mute", 0, 0, 1, True),
    ("Solo", 0, 0, 1, True),
    ("Input Gain", 0, -64, 63, True),
    ("Invert Phase", 0, 0, 1, True),
    ("Insert Pre", 0, 0, 1, True),
    ("Dyn Post EQ", 0, 0, 1, True),
    ("LPF On", 0, 0, 1, True),
    ("HPF On", 0, 0, 1, True),
    ("Filters Dyn S/C", 0, 0, 1, True),
    ("EQ On", 0, 0, 1, True),
    ("EQ E Mode", 0, 0, 1, True),
    ("HF Gain", 0, -64, 63, True),
    ("HF Bell", 0, 0, 1, True),
    ("HF On", 0, 0, 1, False),
    ("HMF Gain", 0, -64, 63, True),
    ("HMF Q", 64, 0, 127, True),
    ("HMF On", 0, 0, 1, False),
    ("LMF Gain", 0, -64, 63, True),
    ("LMF Q", 64, 0, 127, True),
    ("LMF On", 0, 0, 1, False),
    ("LF Gain", 0, -64, 63, True),
    ("LF Bell", 0, 0, 1, True),
    ("LF On", 0, 0, 1, False),
    ("Bypass Insert FX", 0, 0, 1, True),
    ("To Insert FX Peak Meter", 0, 0, 20, False),
    ("From Insert FX Peak Meter", 0, 0, 20, False),
    ("Comp On", 0, 0, 1, True),
    ("C Threshold", 64, 0, 127, True),
    ("C Release", 64, 0, 127, True),
    ("C Ratio", 64, 0, 127, True),
    ("C Peak", 0, 0, 1, True),
    ("C Fast Atk", 0, 0, 1, True),
    ("Comp Gain Reduction", 0, 0, 5, False),
    ("Gate On", 0, 0, 1, True),
    ("G Threshold", 32, 0, 127, True),
    ("G Hold", 0, 0, 127, True),
    ("G Release", 64, 0, 127, True),
    ("G Range", 64, 0, 127, True),
    ("G Fast Atk", 0, 0, 1, True),
    ("Expander", 0, 0, 1, True),
    ("Gate Gain Reduction", 0, 0, 5, False),
    ("Key On", 0, 0, 1, True),
]
for _n in range(1, 9):
    _BP_TABLE += [
        (f"FX{_n} Send On", 0, 0, 1, True),
        (f"FX{_n} Send Level", 63, 0, 127, True),
        (f"FX{_n} Pre Fader", 0, 0, 1, True),
    ]
_BP_TABLE += [
    ("VU Meter", 0, 0, 0, False), ("VU Meter L", 0, 0, 0, False), ("VU Meter R", 0, 0, 0, False),
    ("Input Meter Left", 0, 0, 0, False), ("Input Meter Right", 0, 0, 0, False),
    ("Input Left Peak", 0, 0, 0, False), ("Input Right Peak", 0, 0, 0, False),
    ("Input Clip Indicator Left", 0, 0, 1, False), ("Input Clip Indicator Right", 0, 0, 1, False),
    ("Tuner Enable", 0, 0, 1, True),
    ("Tuner Pitch Detected", 0, 0, 1, False), ("Tuner Note Number", 0, 0, 11, False),
    ("Tuner Pitch Cents", 0, -50, 50, False),
    ("Full Scale Input Meter Left", 0, 0, 0, False), ("Full Scale Input Meter Right", 0, 0, 0, False),
    ("Full Scale Input Left Peak", 0, 0, 0, False), ("Full Scale Input Right Peak", 0, 0, 0, False),
]
assert len(_BP_TABLE) == 84  # static_assert(kBPFullScaleInputLevelMeterRightMax == 83)

# ChannelDeviceProtocol.cpp:240-267 (gLPInfo), index = enum ELParameters
_LP_TABLE: List[Tuple[str, int, int, int, bool]] = [
    ("LPF Frequency", 673, 0, 1000, True),
    ("HPF Frequency", 422, 0, 1000, True),
    ("HF Frequency", 500, 0, 1000, True),
    ("HMF Frequency", 500, 0, 1000, True),
    ("LMF Frequency", 500, 0, 1000, True),
    ("LF Frequency", 500, 0, 1000, True),
    ("Level", 734, 0, 1000, True),
    ("Pan", 0, -100, 100, True),
    ("PDC Insert FX Return Delay Frames", 0, 0, 96000 * 5, False),
    ("PDC Send FX Out Delay Frames", 0, 0, 96000 * 5, False),
    ("Channel Latency Adjust (samples)", 0, -96000 * 5, 96000 * 5, True),
]

CHANNEL_BP: Dict[str, int] = {t[0]: i for i, t in enumerate(_BP_TABLE)}
CHANNEL_LP: Dict[str, int] = {t[0]: i for i, t in enumerate(_LP_TABLE)}
CHANNEL_BP_INFO = {t[0]: dict(index=i, default=t[1], min=t[2], max=t[3], saved=t[4]) for i, t in enumerate(_BP_TABLE)}
CHANNEL_LP_INFO = {t[0]: dict(index=i, default=t[1], min=t[2], max=t[3], saved=t[4]) for i, t in enumerate(_LP_TABLE)}

CHANNEL_DISK_INTERNAL_NAME = "X15 Main Mixer Disk Channel"
CHANNEL_MIX_INTERNAL_NAME = "X15 Main Mixer Mix Channel"
CHANNEL_PARM_VERSION = bytes([0xBC, 0x03, 0x00, 0x00, 0x00])  # observed in a saved song


def channel_default_parm_values() -> Tuple[Dict[int, int], Dict[int, int]]:
    """{bp_id: default}, {lp_id: default} for the params Reason writes into the device PARM
    (kPPSavedInSong only; PropertyUtils.cpp:440-477)."""
    bp = {i: t[1] for i, t in enumerate(_BP_TABLE) if t[4]}
    lp = {i: t[1] for i, t in enumerate(_LP_TABLE) if t[4]}
    return bp, lp


def channel_defaults() -> Dict[str, int]:
    """All channel params by name -> default raw value."""
    d = {t[0]: t[1] for t in _BP_TABLE}
    d.update({t[0]: t[1] for t in _LP_TABLE})
    return d


def channel_params_from_parm(bp: Mapping[int, int], lp: Mapping[int, int]) -> Dict[str, int]:
    """PARM id dicts (signed values) -> name dict, filled up with defaults."""
    d = channel_defaults()
    for i, v in bp.items():
        d[_BP_TABLE[i][0]] = int(v)
    for i, v in lp.items():
        d[_LP_TABLE[i][0]] = int(v)
    return d


def channel_params_to_parm(params: Mapping[str, int]) -> Tuple[Dict[int, int], Dict[int, int]]:
    """Name dict -> ({bp_id: v}, {lp_id: v}) restricted to saved params, defaults for missing."""
    full = channel_defaults()
    full.update(params)
    bp = {i: int(full[t[0]]) for i, t in enumerate(_BP_TABLE) if t[4]}
    lp = {i: int(full[t[0]]) for i, t in enumerate(_LP_TABLE) if t[4]}
    return bp, lp


def _clip_round(x: float, lo: int, hi: int) -> int:
    return int(min(hi, max(lo, int(round(float(x))))))


# ---------------------------------------------------------------------------
# Channel raw <-> physical maps   (ChannelDeviceProtocol.cpp:403-489 unless noted)
# ---------------------------------------------------------------------------
# frequency maps: Hz = base * exp(raw * k), raw 0..1000
FREQ_MAPS: Dict[str, Tuple[float, float]] = {
    "LPF": (100.0, 0.0052983173),   # :403-405   100 Hz .. 20 kHz
    "HPF": (20.0, 0.0052983173),    # :410-412   20 Hz .. 4 kHz
    "HF": (1500.0, 0.002685577),    # :421-423   1.5 kHz .. 22 kHz
    "HMF": (600.0, 0.002456736),    # :428-430   600 Hz .. 7 kHz
    "LMF": (200.0, 0.002302581),    # :435-437   200 Hz .. 2 kHz
    "LF": (40.0, 0.002708050),      # :442-444   40 Hz .. 600 Hz
}


def channel_freq_hz(band: str, raw) -> np.ndarray | float:
    """band in FREQ_MAPS; float32 like the source.  Works on arrays / non-integer raws."""
    base, k = FREQ_MAPS[band.upper()]
    r = np.exp(np.asarray(raw, dtype=F32) * F32(k)) * F32(base)
    return float(r) if np.ndim(r) == 0 else r


def channel_freq_to_raw(band: str, hz: float) -> int:
    """Nearest raw (0..1000).  NB the source's *ToParameterValue truncates (CheckedCast)."""
    base, k = FREQ_MAPS[band.upper()]
    return _clip_round(math.log(max(hz, 1e-9) / base) / k, 0, 1000)


def input_gain_db(raw) -> float:
    """Input Gain: dB = raw*18/63  (-18.29 .. +18 dB).  :417-419"""
    return float(F32(raw) * F32(18.0) / F32(63.0))


def input_gain_db_to_raw(db: float) -> int:
    return _clip_round(db * 63.0 / 18.0, -64, 63)


def input_gain_linear(raw, invert: bool = False) -> float:
    """Linear factor actually applied: exp(dB*0.1155) (NB 0.1155, not ln10/20=0.115129), negated
    if Invert Phase.  DLLChannelEQDSP.cpp:137-145."""
    g = float(np.exp(F32(input_gain_db(raw)) * F32(0.1155)))
    return -g if invert else g


def eq_gain_db(raw) -> float:
    """EQ band gain: dB = raw*20/63  (-20.3 .. +20 dB).  :449-451"""
    return float(F32(raw) * F32(20.0) / F32(63.0))


def eq_gain_db_to_raw(db: float) -> int:
    return _clip_round(db * 63.0 / 20.0, -64, 63)


def eq_q(raw) -> float:
    """HMF/LMF Q knob: 0.7*exp(raw*0.01002335)  (0.70 .. 2.50, default 64 -> 1.33).  :456-458"""
    return float(np.exp(F32(raw) * F32(0.01002335)) * F32(0.7))


def eq_q_to_raw(q: float) -> int:
    return _clip_round(math.log(max(q, 1e-9) / 0.7) / 0.01002335, 0, 127)


def eq_db_to_gain(db: float, dtype=F32) -> float:
    """static dBToGain: exp(dB*0.1156).  :494-496 (also Foundation/RT/EngineDSPInlines.h:25)."""
    return dtype(np.exp(dtype(db) * dtype(0.1156)))


def fader_gain(raw) -> float:
    """Level fader: gain = (raw/734)^3.  AudioVolumeGain.h:35-40 (DecimalGainParameterToGain),
    used at DLLChannelDevice.cpp:2867/2881.  734 = 0 dB, 1000 = +8.06 dB, 0 = -inf."""
    g = F32(raw) * F32(1.0 / 734.0)
    return float(g * g * g)


def fader_db(raw) -> float:
    return -math.inf if raw <= 0 else 60.0 * math.log10(raw / 734.0)


def fader_db_to_raw(db: float) -> int:
    if db == -math.inf:
        return 0
    return _clip_round(734.0 * 10 ** (db / 60.0), 0, 1000)


_KPANLAW = F32(1.4142135)            # DLLChannelDevice.cpp:2447
_KPANC1 = _KPANLAW * F32(2.0) - F32(4.0)   # :2448
_KPANC2 = -_KPANC1 - _KPANLAW              # :2449


def pan_gains(raw) -> Tuple[float, float]:
    """(leftPan, rightPan) for Pan raw -100..100.  DLLChannelDevice.cpp:2790-2802.

    p = clip(raw/200 + 0.5, 0, 1);  leftPan = C1*p^2 + C2*p + sqrt2, rightPan = same with 1-p,
    C1 = 2*sqrt2 - 4, C2 = 4 - 3*sqrt2.  Centre = 1.0/1.0 (0 dB), hard side = sqrt2 (+3 dB) / 0.
    """
    pan = F32(raw) / F32(100.0)
    pan = F32(min(max(pan * F32(0.5) + F32(0.5), F32(0.0)), F32(1.0)))
    inv = F32(1.0) - pan
    lp = pan * pan * _KPANC1 + pan * _KPANC2 + _KPANLAW
    rp = inv * inv * _KPANC1 + inv * _KPANC2 + _KPANLAW
    return float(lp), float(rp)


def pan_to_raw(position: float) -> int:
    """position -1 (hard L) .. +1 (hard R) -> raw -100..100."""
    return _clip_round(position * 100.0, -100, 100)


def width_factor(raw) -> float:
    """w = raw/127 * 0.5 (0 .. 0.5).  DLLChannelDevice.cpp:2785-2786; EnginePropertySet.h:405."""
    return float(F32(raw) * F32(1.0 / 127.0) * F32(0.5))


def width_to_raw(fraction: float) -> int:
    """fraction 0 (mono) .. 1 (full stereo) -> raw 0..127."""
    return _clip_round(fraction * 127.0, 0, 127)


def pan_width_matrix(pan_raw, width_raw, mute: bool = False) -> np.ndarray:
    """2x2 matrix M with [outL, outR] = M @ [inL, inR]  (pre-fader).  DLLChannelDevice.cpp:2836-2842.

    LL = leftPan*(0.5+w), RL = leftPan*(0.5-w), LR = rightPan*(0.5-w), RR = rightPan*(0.5+w).
    """
    lp, rp = (F32(v) for v in pan_gains(pan_raw))
    w = F32(width_factor(width_raw))
    m = F32(0.0) if mute else F32(1.0)
    h = F32(0.5)
    return np.array([[lp * (h + w) * m, lp * (h - w) * m],
                     [rp * (h - w) * m, rp * (h + w) * m]], dtype=np.float64)


# ---------------------------------------------------------------------------
# Channel filter / EQ coefficient ports
# ---------------------------------------------------------------------------

def calc_parametric_constants(f_norm, q, gain_db, emode: bool, dtype=F32):
    """Port of NSChannelParameters::CalcParametricConstants  (ChannelDeviceProtocol.cpp:498-520).
    f_norm = Hz/sr.  Returns (A, B, C) as used by RenderParEQFilter (gain=A, fb2=B, fb1=C)."""
    T = dtype
    posGain = T(np.exp(T(gain_db) * T(0.1156)))
    iQ = T(q)
    if emode:
        if posGain < T(1.0):
            iQ = iQ * posGain
    else:
        if posGain > T(1.0):
            iQ = iQ * posGain * T(0.1)
        else:
            iQ = iQ * T(0.1)
    w = T(3.141592654) * T(2.0) * T(f_norm)
    w = min(w, T(3.0))
    b = (iQ - w * T(0.5)) / (T(2.0) * iQ + w)
    c = (T(0.5) + b) * T(np.cos(w))
    a = (T(0.5) - b) * (posGain - T(1.0))
    return a, b * T(2.0), c * T(2.0)


def calc_lpf_constants(f_norm, dtype=F32):
    """ChannelDeviceProtocol.cpp:522-532 -> (fb1, fb2, gain)."""
    T = dtype
    q = T(1.56)
    w = T(3.141592654) * T(2.0) * T(f_norm)
    w = min(w, T(3.141592654))
    if w > T(1.2):
        q = q * (T(1.0) + (w - T(1.2)) * T(0.54))
    fb2 = (q - w) / (q + w)
    fb1 = (T(1.0) + fb2) * T(np.cos(w))
    gain = ((T(1.0) - fb1) + fb2) * T(0.5)
    return fb1, fb2, gain


def calc_hpf_constants(f_norm, dtype=F32):
    """ChannelDeviceProtocol.cpp:534-542 -> (fb1, fb2, fb3)."""
    T = dtype
    q = T(2.0)
    w = T(3.141592654) * T(2.0) * T(f_norm)
    w = min(w, T(1.57))
    fb2 = (q - w) / (q + w)
    fb1 = (T(1.0) + fb2) * T(np.cos(w))
    fb3 = T(1.0) - w
    return fb1, fb2, fb3


_EQ_SILENT = 1.0 / 20000.0  # DLLChannelEQDSP.cpp:24 kEQSignalSilentLimit


def _silent(x) -> bool:
    return -_EQ_SILENT < float(x) < _EQ_SILENT


def _p(params: Optional[Mapping[str, float]]) -> Dict[str, float]:
    d: Dict[str, float] = dict(channel_defaults())
    if params:
        for k, v in params.items():
            if k not in d:
                raise KeyError(f"unknown channel parameter {k!r}")
            d[k] = v
    return d


def channel_filter_sections(params: Optional[Mapping[str, float]], sr: float, dtype: type = F32,
                            include_filters: bool = True, gate: bool = True) -> List[Tuple[str, np.ndarray, np.ndarray]]:
    """Transfer functions (name, b, a) of the HPF/LPF + EQ, in render order.

    Order (DLLChannelDevice.cpp:1133-1146 + DLLChannelEQDSP.cpp:253-326, 826-938):
      LPF -> HPF -> LF shelf -> HF shelf -> HF bell -> LF bell -> LMF -> HMF.
    Filters belong to the EQ section only when 'Filters Dyn S/C' == 0
    (DLLChannelDevice.cpp:2387-2394); otherwise they only filter the dynamics key.
    Raw values may be non-integer (used by the fitter).  gate=False disables the source's
    'band is silent if |A| < 1/20000' bypass (DLLChannelEQDSP.cpp:599 etc.) so the fitter has
    a gradient around 0 dB; with integer raws the gated bands are within ~1e-3 dB of unity anyway.
    """
    silent = _silent if gate else (lambda v: False)
    P = _p(params)
    T = dtype
    sr_scale = T(1.0) / T(sr)
    out: List[Tuple[str, np.ndarray, np.ndarray]] = []

    if include_filters and not P["Filters Dyn S/C"]:
        if P["LPF On"]:
            fb1, fb2, g = calc_lpf_constants(T(channel_freq_hz("LPF", P["LPF Frequency"])) * sr_scale, T)
            # RenderLPF: sig = g*x + fb1*d1 - fb2*d2; y = sig + d1    (DLLChannelEQDSP.cpp:201-206)
            out.append(("LPF", np.array([g, g], float), np.array([1.0, -fb1, fb2], float)))
        if P["HPF On"]:
            fb1, fb2, fb3 = calc_hpf_constants(T(channel_freq_hz("HPF", P["HPF Frequency"])) * sr_scale, T)
            # RenderHPF (DLLChannelEQDSP.cpp:210-220): (1-z^-1) * (1-z^-1)/(1-fb1 z^-1+fb2 z^-2)
            #                                           * (1-z^-1)/(1-fb3 z^-1)
            b = np.array([1.0, -3.0, 3.0, -1.0])
            a = np.convolve([1.0, -float(fb1), float(fb2)], [1.0, -float(fb3)])
            out.append(("HPF", b, a))

    # ---- RenderChannelEQFS512  (DLLChannelEQDSP.cpp:578-733)
    eq_on = bool(P["EQ On"])
    emode = bool(P["EQ E Mode"])
    lf_db = eq_gain_db(P["LF Gain"]) if eq_on else 0.0
    lmf_db = eq_gain_db(P["LMF Gain"]) if eq_on else 0.0
    hmf_db = eq_gain_db(P["HMF Gain"]) if eq_on else 0.0
    hf_db = eq_gain_db(P["HF Gain"]) if eq_on else 0.0
    lf_bell = bool(P["LF Bell"])
    hf_bell = bool(P["HF Bell"])
    lf_hz = T(channel_freq_hz("LF", P["LF Frequency"]))
    hf_hz = T(channel_freq_hz("HF", P["HF Frequency"]))
    lmf_hz = T(channel_freq_hz("LMF", P["LMF Frequency"]))
    hmf_hz = T(channel_freq_hz("HMF", P["HMF Frequency"]))
    lmf_q = T(eq_q(P["LMF Q"]))
    hmf_q = T(eq_q(P["HMF Q"]))

    def bell(name, fhz, q, gdb, em):
        A, B, C = calc_parametric_constants(fhz * sr_scale, q, gdb, em, T)
        if silent(A):
            return None
        # RenderParEQFilter: s = A x + C s1 - B s2; y = x + s - s2   (:430-435)
        return (name, np.array([1.0 + A, -C, B - A], float), np.array([1.0, -C, B], float))

    # LF  (:595-638)
    lf_bell_sec = bell("LF bell", lf_hz, T(2.5), T(lf_db if lf_bell else 0.0), True)
    freq = lf_hz * sr_scale * T(-40.0)
    gain = T(0.0) if lf_bell else T(lf_db) * freq * T(0.915) / T(20.0)
    if gain > 0:
        lA, lB = T(np.exp(freq + gain)), T(np.exp(freq))
    else:
        lA, lB = T(np.exp(freq)), T(np.exp(freq - gain))
    # RenderShelvingFilter: sig = x - fb*x1; y = sig + ff*y1  (fb=A, ff=B)  (:451-456)
    lf_shelf = None if silent(lA - lB) else ("LF shelf", np.array([1.0, -lA], float), np.array([1.0, -lB], float))

    # HF  (:640-691)
    hf_bell_sec = bell("HF bell", hf_hz, T(2.96), T(hf_db if hf_bell else 0.0), True)
    hfreq = T(np.exp(hf_hz * sr_scale * T(-6.0)))
    inv = T(1.0) - hfreq
    hgain = T(0.0) if hf_bell else T(hf_db) * T(0.9) / T(20.0)
    add = inv * hgain
    if hgain > 0:
        hA = hfreq + add
        hB = hfreq
        hC = inv / (T(1.0) - hA)
    else:
        hA = hfreq
        hB = hfreq - add
        hC = (T(1.0) / inv) * (T(1.0) - hB)
    # input scaled by C before the shelving filter (:880-881)
    hf_shelf = None if silent(hA - hB) else (
        "HF shelf", np.array([hC, -hC * hA], float), np.array([1.0, -hB], float))

    lmf = bell("LMF", lmf_hz, lmf_q, T(lmf_db), emode)   # :693-709
    hmf = bell("HMF", hmf_hz, hmf_q, T(hmf_db), emode)   # :711-727

    for sec in (lf_shelf, hf_shelf, hf_bell_sec, lf_bell_sec, lmf, hmf):  # render order :837-937
        if sec is not None:
            out.append(sec)
    return out


def _sections_response(sections, freqs, sr) -> np.ndarray:
    w = 2 * np.pi * np.asarray(freqs, float) / float(sr)
    zi = np.exp(-1j * w)
    h = np.ones_like(zi)
    for _, b, a in sections:
        nb = np.polyval(b[::-1], zi)  # sum b_k z^-k
        na = np.polyval(a[::-1], zi)
        h = h * nb / na
    return h


def channel_eq_response(params: Optional[Mapping[str, float]], freqs, sr: float,
                        include_filters: bool = True, include_gains: bool = False) -> np.ndarray:
    """dB magnitude of HPF/LPF + 4-band EQ at `freqs` (Hz).  With include_gains, also adds input gain,
    and fader for a centred mono source (pan law centre = 0 dB)."""
    P = _p(params)
    h = _sections_response(channel_filter_sections(P, sr, include_filters=include_filters), freqs, sr)
    db = 20 * np.log10(np.maximum(np.abs(h), 1e-30))
    if include_gains:
        db = db + 20 * math.log10(abs(input_gain_linear(P["Input Gain"]))) + (
            fader_db(P["Level"]) if P["Level"] > 0 else -600.0)
    return db


# ---------------------------------------------------------------------------
# Channel signal path
# ---------------------------------------------------------------------------

def channel_process(x, sr: float, params: Optional[Mapping[str, float]] = None,
                    insert: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> np.ndarray:
    """Render one channel strip with static parameters.  Returns (n, 2) float64 (main output).

    x: (n,) mono or (n, 2) stereo.  Mono = only Left input connected (Mix Channel,
    DLLChannelDevice.cpp:796-842) or a mono recording on a Disk Channel (:1296-1302).
    A mono channel is processed on one buffer and duplicated L=R after the EQ/dyn sections
    (:1398-1403, :1715-1719), so Width has no effect on it and Pan gives L=leftPan, R=rightPan.

    Order (DLLChannelDevice.cpp:1346-1395, 1661-1745, 1747-1895, 2385-2417):
      input gain & phase -> [dynamics, EQ(filters+EQ)] (or [EQ, dyn] if 'Dyn Post EQ')
      -> insert FX -> (sections here instead if 'Insert Pre') -> mute/pan/width -> fader.
    Dynamics are NOT modelled (skipped).  Parameter smoothing / per-batch ramps are ignored
    (static parameters => steady state is identical).  insert: callable (n,2)->(n,2) or None.
    """
    P = _p(params)
    x = np.asarray(x, dtype=np.float64)
    stereo = x.ndim == 2
    if stereo and x.shape[1] != 2:
        raise ValueError("stereo input must be shaped (n, 2)")
    chans = [x[:, 0].copy(), x[:, 1].copy()] if stereo else [x.copy()]

    g_in = input_gain_linear(P["Input Gain"], bool(P["Invert Phase"]))
    chans = [c * g_in for c in chans]

    secs = channel_filter_sections(P, sr)

    def run_sections(cs):
        out = []
        for c in cs:
            for _, b, a in secs:
                c = _sig.lfilter(b, a, c)
            out.append(c)
        return out

    if not P["Insert Pre"]:
        chans = run_sections(chans)
    if len(chans) == 1:
        chans = [chans[0], chans[0].copy()]
    y = np.stack(chans, axis=1)
    if insert is not None and not P["Bypass Insert FX"]:
        y = np.asarray(insert(y), dtype=np.float64)
    if P["Insert Pre"]:
        if stereo:
            y = np.stack(run_sections([y[:, 0], y[:, 1]]), axis=1)
        else:  # insert returned stereo but channel mono: :1631-1640 treats as stereo if FX R connected
            y = np.stack(run_sections([y[:, 0], y[:, 1]]), axis=1)

    M = pan_width_matrix(P["Pan"], P["Width"], bool(P["Mute"]))
    y = y @ M.T
    return y * fader_gain(P["Level"])


# ---------------------------------------------------------------------------
# EQ fitting
# ---------------------------------------------------------------------------
_FIT_KEYS = ["LF Gain", "LF Frequency", "LMF Gain", "LMF Frequency", "LMF Q",
             "HMF Gain", "HMF Frequency", "HMF Q", "HF Gain", "HF Frequency"]
_FIT_BOUNDS = {"LF Gain": (-64, 63), "LMF Gain": (-64, 63), "HMF Gain": (-64, 63), "HF Gain": (-64, 63),
               "LF Frequency": (0, 1000), "LMF Frequency": (0, 1000), "HMF Frequency": (0, 1000),
               "HF Frequency": (0, 1000), "LMF Q": (0, 127), "HMF Q": (0, 127), "HPF Frequency": (0, 1000)}


def fit_channel_eq(target_db, freqs, sr: float = 48000.0, fit_hpf: bool = True,
                   emode: Optional[bool] = None, shelf_modes: Optional[Sequence[Tuple[bool, bool]]] = None,
                   weights=None, fixed: Optional[Mapping[str, int]] = None) -> Dict[str, object]:
    """Least-squares fit of the 4-band EQ (+ optional HPF) raw values to `target_db` at `freqs`.

    target_db: array (same shape as freqs) or callable freqs->dB.  Tries LF/HF bell-vs-shelf
    combinations (default all 4), E-mode on/off (unless `emode` is given), HPF on/off.
    Optimises continuous raws (float64 coefficient math), rounds, then does an integer
    coordinate-descent polish against the exact float32 response.
    Returns dict(params=name->raw int, residual_db=array, rms_db=float, max_abs_db=float).
    """
    from scipy.optimize import least_squares

    freqs = np.asarray(freqs, float)
    tgt = np.asarray(target_db(freqs) if callable(target_db) else target_db, float)
    wts = np.ones_like(freqs) if weights is None else np.asarray(weights, float)
    fixed = dict(fixed or {})
    shelf_modes = list(shelf_modes) if shelf_modes is not None else [(False, False), (True, False), (False, True), (True, True)]
    emodes = [emode] if emode is not None else [False, True]
    hpfs = [False, True] if fit_hpf else [False]

    def make_params(keys, vec, base):
        p = dict(base)
        for k, v in zip(keys, vec):
            p[k] = float(v)
        return p

    def resp64(p):
        h = _sections_response(channel_filter_sections(p, sr, dtype=np.float64, gate=False), freqs, sr)
        return 20 * np.log10(np.maximum(np.abs(h), 1e-30))

    def resp32(p):
        return channel_eq_response(p, freqs, sr)

    best = None
    for em in emodes:
        for lf_bell, hf_bell in shelf_modes:
            for hp in hpfs:
                base = dict(channel_defaults())
                base.update({"EQ On": 1, "EQ E Mode": int(em), "LF Bell": int(lf_bell), "HF Bell": int(hf_bell),
                             "HPF On": int(hp), "LPF On": 0, "Filters Dyn S/C": 0})
                base.update(fixed)
                keys = [k for k in _FIT_KEYS + (["HPF Frequency"] if hp else []) if k not in fixed]
                lo = np.array([_FIT_BOUNDS[k][0] for k in keys], float)
                hi = np.array([_FIT_BOUNDS[k][1] for k in keys], float)
                starts = []
                s0 = np.array([0.5 if "Gain" in k else (64.0 if " Q" in k else (100.0 if k == "HPF Frequency" else 500.0)) for k in keys])
                starts.append(s0)
                s1 = s0.copy()
                for i, k in enumerate(keys):
                    if k in ("LMF Frequency", "HMF Frequency"):
                        s1[i] = 250.0 if k == "LMF Frequency" else 750.0
                starts.append(s1)
                for s in starts:
                    fun = lambda v: (resp64(make_params(keys, v, base)) - tgt) * wts
                    try:
                        r = least_squares(fun, np.clip(s, lo, hi), bounds=(lo, hi), diff_step=1e-3, max_nfev=400)
                    except Exception:  # pragma: no cover - numerical failures just skip this start
                        continue
                    cand = {k: _clip_round(v, *_FIT_BOUNDS[k]) for k, v in zip(keys, r.x)}
                    p = dict(base); p.update(cand)
                    err = float(np.sqrt(np.mean(((resp32(p) - tgt) * wts) ** 2)))
                    if best is None or err < best[0]:
                        best = (err, p, keys)

    err, p, keys = best
    # integer coordinate-descent polish on the exact (float32) response
    def cost(pp):
        return float(np.sqrt(np.mean(((resp32(pp) - tgt) * wts) ** 2)))
    improved = True
    passes = 0
    while improved and passes < 6:
        improved = False
        passes += 1
        for k in keys:
            for step in (8, 3, 1, -1, -3, -8):
                q = dict(p)
                q[k] = int(min(_FIT_BOUNDS[k][1], max(_FIT_BOUNDS[k][0], p[k] + step)))
                c = cost(q)
                if c < err - 1e-9:
                    p, err, improved = q, c, True
    res = resp32(p) - tgt
    out_params = {k: int(v) for k, v in p.items()
                  if k in _FIT_KEYS or k in ("HPF Frequency", "HPF On", "LPF On", "EQ On", "EQ E Mode",
                                             "LF Bell", "HF Bell", "Filters Dyn S/C")}
    return dict(params=out_params, residual_db=res, rms_db=float(np.sqrt(np.mean(res ** 2))),
                max_abs_db=float(np.max(np.abs(res))))


# ---------------------------------------------------------------------------
# MClass Stereo Imager   ("MClass Stereo StereoImager")
# ---------------------------------------------------------------------------
IMAGER_INTERNAL_NAME = "MClass Stereo StereoImager"
# MStereoImagerProtocol.h:55 + MStereoImagerProtocol.cpp:23-44; kBPEffectMode = 0 (EffectProtocol.h:14)
IMAGER_BP = {"Enabled": 0, "Low Width": 1, "High Width": 2, "X-Over Frequency": 3, "Solo Mode": 4,
             "Separate Out Mode": 5, "Peak Meter": 6, "Low Width Meter": 7, "High Width Meter": 8}
IMAGER_DEFAULTS = {"Enabled": 1, "Low Width": 0, "High Width": 0, "X-Over Frequency": 64, "Solo Mode": 0,
                   "Separate Out Mode": 0}
# Low/High Width, X-Over, Solo are kPPSavedInPatch => stored in FORM PTCH / PARM, not the device PARM.
IMAGER_PATCH_PARAMS = ("Low Width", "High Width", "X-Over Frequency", "Solo Mode")


def imager_xover_hz(raw) -> float:
    """100*exp(raw*0.032239): 0 -> 100 Hz, 64 -> 787 Hz, 127 -> 6.0 kHz.  MStereoImagerProtocol.cpp:60-62"""
    return float(np.exp(F32(raw) * F32(0.032239)) * F32(100.0))


def imager_xover_to_raw(hz: float) -> int:
    return _clip_round(math.log(max(hz, 1e-9) / 100.0) / 0.032239, 0, 127)


def imager_width_factor(raw) -> float:
    """w = raw/64 (-1 .. 0.984).  DLLMStereoImager.cpp:160-161"""
    return float(F32(raw) / F32(64.0))


def imager_width_to_raw(w: float) -> int:
    return _clip_round(w * 64.0, -64, 63)


def imager_band_matrix(raw) -> np.ndarray:
    """2x2 per-band matrix (DLLMStereoImager.cpp:227-242, 270-289), [L',R'] = M @ [L,R].
    w>0: L' = L(1-w/2) + (L-R)w ; R' = R(1-w/2) - (L-R)w.   w<=0: L' = L(1+w/2) + R(-w/2), sym."""
    w = F32(imager_width_factor(raw))
    if w > 0:
        dry = F32(1.0) - w * F32(0.5)
        return np.array([[dry + w, -w], [-w, dry + w]], float)
    hw = w * F32(-0.5)
    dry = F32(1.0) + w * F32(0.5)
    return np.array([[dry, hw], [hw, dry]], float)


def imager_mid_side_gains(raw) -> Tuple[float, float]:
    """(mid gain, side gain) for a band.  w>0: (1-w/2, 1+1.5w);  w<=0: (1, 1+w)."""
    M = imager_band_matrix(raw)
    return float(M[0, 0] + M[0, 1]), float(M[0, 0] - M[0, 1])


def imager_lpf_coeffs(xover_raw, sr: float):
    """(a, b, gain) for RenderLPF12: y = g x + a y1 - b y2.  DLLMStereoImager.cpp:153-158 (q = 0.735603)."""
    f = F32(imager_xover_hz(xover_raw))
    tw = F32(2.0 * math.pi) * f / F32(sr)
    q = F32(0.735603)
    a = F32(2.0) * F32(np.cos(tw)) * F32(np.exp(F32(-0.5) * tw / q))
    b = F32(np.exp(-tw / q))
    g = (F32(1.0) - a) + b
    return a, b, g


def imager_process(x, sr: float, low_width_raw: int = 0, high_width_raw: int = 0, xover_raw: int = 64,
                   solo_mode: int = 0, return_separate: bool = False, separate_out_mode: int = 0):
    """Port of CDLLMStereoImager::RenderStereo (DLLMStereoImager.cpp:210-346).
    x (n,2) or (n,) (mono: only Left connected => R := L, :405-411).  Returns (n,2) main out
    (and the separate-out pair if return_separate: low band when separate_out_mode == 0
    (kSeparateOutModeLowBand, the default), else high band -- :308-315)."""
    x = np.asarray(x, float)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    a, b, g = imager_lpf_coeffs(xover_raw, sr)
    lp = _sig.lfilter([float(g)], [1.0, -float(a), float(b)], x, axis=0)
    hp = x - lp
    hi = hp @ imager_band_matrix(high_width_raw).T
    lo = lp @ imager_band_matrix(low_width_raw).T
    main = {0: hi + lo, 1: lo, 2: hi}[int(solo_mode)]
    sep = lo if int(separate_out_mode) == 0 else hi
    return (main, sep) if return_separate else main


# ---------------------------------------------------------------------------
# MClass Maximizer
# ---------------------------------------------------------------------------
MAXIMIZER_INTERNAL_NAME = "MClass Maximizer"
# MMaximizerProtocol.h:66-80, MMaximizerProtocol.cpp:24-55
MAXIMIZER_BP = {"Enabled": 0, "Input Gain": 1, "Limiter Enable": 2, "Look Ahead Enable": 3, "Attack Speed": 4,
                "Release Speed": 5, "Output Gain": 6, "Soft Clip Enable": 7, "Soft Clip Amount": 8,
                "Output Level Meter Mode": 9, "Peak Meter": 10, "Gain Meter": 11, "Output Level Left": 12,
                "Output Level Right": 13}
MAXIMIZER_DEFAULTS = {"Enabled": 1, "Input Gain": 64, "Limiter Enable": 1, "Look Ahead Enable": 0,
                      "Attack Speed": 0, "Release Speed": 0, "Output Gain": 64, "Soft Clip Enable": 0,
                      "Soft Clip Amount": 64, "Output Level Meter Mode": 0}
MAXIMIZER_ATTACK_MS = {0: 4, 1: 16, 2: 64}       # Fast/Mid/Slow  MMaximizerProtocol.cpp:91-108
MAXIMIZER_RELEASE_MS = {0: 150, 1: 400, 2: 150}  # Fast/Slow/Auto(150 + adaptive)  :72-89


def maximizer_gain_db(raw) -> float:
    """Input/Output Gain: dB = raw*12/64 - 12  (0 -> -12, 64 -> 0, 127 -> +11.81).  MMaximizerProtocol.cpp:66-71.
    Raw is 0..127 (NOT signed)."""
    return float(F32(raw) * F32(12.0) / F32(64.0) - F32(12.0))


def maximizer_gain_db_to_raw(db: float) -> int:
    return _clip_round((db + 12.0) * 64.0 / 12.0, 0, 127)


def maximizer_input_gain_linear(raw) -> float:
    """exp(dB*0.1156)  (DLLMMaximizer.cpp:193, EngineDSPInlines.h:25)."""
    return float(eq_db_to_gain(maximizer_gain_db(raw)))


def maximizer_output_gain_linear(raw) -> float:
    """exp(dB*0.1156) * 0.99   (DLLMMaximizer.cpp:194-196)."""
    return float(eq_db_to_gain(maximizer_gain_db(raw)) * F32(0.99))


def maximizer_lookahead_samples(sr: float, on: bool) -> int:
    """int(0.004*sr) when on (DLLMMaximizer.cpp:198-206).  Effective delay is this + 1 samples
    (ring index wraps when > L, :291-294)."""
    return int(F32(0.004) * F32(sr)) if on else 0


def maximizer_threshold(lookahead_on: bool) -> float:
    """Limiter threshold (linear): 0.93 with look-ahead, 0.8 without (DLLMMaximizer.cpp:200,205)."""
    return 0.93 if lookahead_on else 0.8


def maximizer_soft_clip_amount(raw) -> float:
    """raw/127*0.65 + 0.01   (DLLMMaximizer.cpp:211-214)."""
    return float(max(F32(raw) * F32(1.0 / 127.0) * F32(0.65) + F32(0.01), F32(0.01)))


def _soft_clip(x: np.ndarray, amount: float) -> np.ndarray:
    """DLLMMaximizer.cpp:402-436: out = (f(x+a) - f(x-a)) * 0.49/a,  f(u)=|u|+1.5 for |u|>1 else 2+u^2/2."""
    def f(u):
        au = np.abs(u)
        return np.where(au > 1.0, au + 1.5, 2.0 + u * u * 0.5)
    return (f(x + amount) - f(x - amount)) * (0.49 / amount)


def maximizer_process(x, sr: float, input_gain_raw: int = 64, limiter_on: int = 1, lookahead_on: int = 0,
                      attack_raw: int = 0, release_raw: int = 0, output_gain_raw: int = 64,
                      softclip_on: int = 0, softclip_amount_raw: int = 64) -> np.ndarray:
    """Port of RenderOneChannel (DLLMMaximizer.cpp:233-436).  L and R are limited INDEPENDENTLY
    (separate VDelays per render unit, :552/:608) -- the limiter is not stereo-linked.
    Fidelity: op-for-op for static parameters, initial state = VDelays::Clear() (peak 0, env 1).
    Ignores the 64-sample smoothing of gain changes (CSmoothParameter2).  Pure-Python sample loop
    (slow: ~seconds per 10 s of stereo audio).  x: (n,) or (n,2)."""
    x = np.asarray(x, float)
    mono = x.ndim == 1
    X = x[:, None] if mono else x
    T = F32
    att = float(T(1.0) - T(np.exp(T(-4.6) / (T(MAXIMIZER_ATTACK_MS[int(attack_raw)] * 0.001) * T(sr)))))
    rel_sec = MAXIMIZER_RELEASE_MS[int(release_raw)] * 0.001
    adaptive = int(release_raw) == 2
    ad_freq = float(np.exp(T(-10.0) / T(sr)))
    ad_gain = (1.0 - ad_freq) ** 2
    pk_rel = float(T(1.0) - T(np.exp(T(-4.6) / (T(0.08) * T(sr)))))
    gin = maximizer_input_gain_linear(input_gain_raw)
    gout = maximizer_output_gain_linear(output_gain_raw)
    L = maximizer_lookahead_samples(sr, bool(lookahead_on))
    thr = maximizer_threshold(bool(lookahead_on))
    mid = L // 2
    out = np.empty_like(X)
    for ch in range(X.shape[1]):
        s = X[:, ch] * gin
        if limiter_on:
            buf = [0.0] * 1024
            idx = 0
            peak, env, ad1, ad2 = 0.0, 1.0, 0.0, 0.0
            res = np.empty_like(s)
            sl = s.tolist()
            n = len(sl)
            for start in range(0, n, KBATCH):
                if adaptive:
                    off = min(max((ad2 * ad_gain - 0.4) * 0.4, 0.0), 0.3)
                    rel = 1.0 - math.exp(-4.6 / ((off + rel_sec) * sr))
                else:
                    rel = 1.0 - math.exp(-4.6 / (rel_sec * sr))
                for c in range(start, min(start + KBATCH, n)):
                    smp = sl[c]
                    key = abs(smp)
                    if adaptive:
                        ad1 = key + ad1 * ad_freq
                        ad2 = ad1 + ad2 * ad_freq
                    if L > 0:
                        din = smp
                        smp = buf[idx]
                        buf[idx] = din
                        mi = idx - mid
                        if mi < 0:
                            mi += L
                        mk = abs(buf[mi])
                        if mk > key:
                            key = mk
                        idx += 1
                        if idx > L:
                            idx = 0
                    if key > peak:
                        peak = key
                    else:
                        peak += (key - peak) * pk_rel
                    tgt = 1.0 if peak < thr else thr / peak
                    d = tgt - env
                    env += d * (rel if d > 0 else att)
                    res[c] = smp * env * gout
            s = res
        else:
            # limiter off: output gain is not applied at all in RenderOneChannel (only inside the limiter loop)
            pass
        if softclip_on:
            s = _soft_clip(s, maximizer_soft_clip_amount(softclip_amount_raw))
        out[:, ch] = s
    return out[:, 0] if mono else out
