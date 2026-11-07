# Reason devices: parameter maps and DSP (from the C++ source)

Module: `mixlab/reason_devices.py`. Tests: `tests/test_reason_devices.py`.

Source paths are relative to `reason/Reason/Src` unless they start with `Core/`, which means `reason/Core/Core/`. The Python port is source-derived only. It has **not been checked against audio rendered by Reason**. That check needs a bounce. Where this doc says "exact port", the code was translated op for op and the coefficients are computed in float32, the same as the C++. Filtering runs in float64. The port ignores the per-batch parameter ramps, because with static parameters they have no effect in steady state.

Confidence values:
- **exact port**: transcribed from the source.
- **inferred**: deduced from the source, but not a direct transcription.
- **not modelled**: the parameter is read, but its DSP is not implemented.
- **unknown**: the meaning is unclear.

## PARM storage

- Layout (`Support/Properties/PropertyUtils.cpp:480-500`):
  1. 5-byte version.
  2. `u8 nBP`, then `nBP × (u8 id, u8 value)`.
  3. `u8 nLP`, then `nLP × (u8 id, u32 BE value)`.

  Only parameters whose persistence matches the chunk are written (`:440-477`). The device `PARM` holds `kPPSavedInSong`. `FORM PTCH/PARM` holds `kPPSavedInPatch`. Parameters are written in ascending id order, and the id is the enum index.
- **Negative values are stored as two's complement, with no offset.** `TByteParameterValue` is `std::int8_t` and `TLongParameterValue` is `std::int32_t` (`Core/Core/Jukebox/PropertyPackage/PropertyPackage.h:63-64`). The value is cast to `uint8` or `uint32` when written (`Support/Properties/PersistantPropertyValue.cpp:47-55, 99-107`). For example, Input Gain −64 is stored as `0xC0`, and Pan −100 as `0xFFFFFF9C`.
- The MClass Imager and Maximizer knobs are `kPPSavedInPatch`. They therefore live in the device's `FORM PTCH` → `PARM`, not in the device-level `PARM`. Only Enabled and Separate Out Mode (imager) are saved in the song. The Maximizer gains are defined as 0..127 with 64 = 0 dB, so they are never negative.
- Helpers: `encode_/decode_byte_value`, `encode_/decode_long_value`, `parse_parm`, `build_parm`, `channel_default_parm_values`, `channel_params_from_parm`, `channel_params_to_parm`.

## 1. Mix/Disk Channel (`X15 Main Mixer Disk Channel` / `X15 Main Mixer Mix Channel`)

Both channel types use one protocol (`Devices/MainMixer/ChannelDevice/ChannelDeviceProtocol.{h,cpp}`).

- Byte parameters: `gBPInfo`, `ChannelDeviceProtocol.cpp:34-234`, enum `EBParameters` in `.h`.
- Long parameters: `gLPInfo`, `:240-267`, enum `ELParameters`.

The observed saved defaults match these tables:
- Width = 127.
- Q = 64.
- LP {0:673, 1:422, 2-5:500, 6:734, 7:0, 10:0}.

LP 8 and 9 are not saved. These defaults are checked by tests.

### Byte parameters (index = id in PARM)

| idx | name | default | range | saved | physical | source | confidence |
|---|---|---|---|---|---|---|---|
| 0 | Width | 127 | 0..127 | Y | w = raw/127·0.5 → pan/width matrix (see §Pan/width) | DLLChannelDevice.cpp:2785-2786, 2836-2842 | exact port |
| 1 | Mute | 0 | 0..1 | Y | muteGain 0/1 in pan matrix (solo logic ignored) | DLLChannelDevice.cpp:2768-2783, 2839-2842 | exact port (mute only) |
| 2 | Solo | 0 | 0..1 | Y | solo bus logic (not modelled) | DLLChannelDevice.cpp:2771-2781 | not modelled |
| 3 | Input Gain | 0 | -64..63 | Y | dB = raw·18/63; applied as exp(dB·0.1155) | ChannelDeviceProtocol.cpp:417-419; DLLChannelEQDSP.cpp:137-145 | exact port |
| 4 | Invert Phase | 0 | 0..1 | Y | negates the input gain | DLLChannelEQDSP.cpp:141-143 | exact port |
| 5 | Insert Pre | 0 | 0..1 | Y | 1 = dyn/EQ sections run after inserts | DLLChannelDevice.cpp:2399-2416 | exact port (order) |
| 6 | Dyn Post EQ | 0 | 0..1 | Y | 0 = dyn→EQ, 1 = EQ→dyn | DLLChannelDevice.cpp:2397-2416 | exact port (order; dyn skipped) |
| 7 | LPF On | 0 | 0..1 | Y | enables 12 dB/oct LPF | DLLChannelEQDSP.cpp:148-194, 201-206 | exact port |
| 8 | HPF On | 0 | 0..1 | Y | enables 18 dB/oct HPF | DLLChannelEQDSP.cpp:148-194, 210-220 | exact port |
| 9 | Filters Dyn S/C | 0 | 0..1 | Y | 1 = HPF/LPF filter only the dynamics key, NOT the audio | DLLChannelDevice.cpp:2387-2394, 1106-1124 | exact port |
| 10 | EQ On | 0 | 0..1 | Y | 0 = all 4 band gains forced to 0 dB (filters unaffected) | DLLChannelEQDSP.cpp:586-591 | exact port |
| 11 | EQ E Mode | 0 | 0..1 | Y | LMF/HMF Q scaling: E: Q·g if cut; G(0): Q·0.1·max(g,1) | ChannelDeviceProtocol.cpp:498-512 | exact port |
| 12 | HF Gain | 0 | -64..63 | Y | dB = raw·20/63 | ChannelDeviceProtocol.cpp:449-451; DLLChannelEQDSP.cpp:640-691 | exact port |
| 13 | HF Bell | 0 | 0..1 | Y | 0 = shelf, 1 = bell (Q 2.96) | DLLChannelEQDSP.cpp:642-673 | exact port |
| 14 | HF On | 0 | 0..1 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 15 | HMF Gain | 0 | -64..63 | Y | dB = raw·20/63 | ChannelDeviceProtocol.cpp:449-451 | exact port |
| 16 | HMF Q | 64 | 0..127 | Y | Q = 0.7·exp(raw·0.01002335) (0.70–2.50) | ChannelDeviceProtocol.cpp:456-458 | exact port |
| 17 | HMF On | 0 | 0..1 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 18 | LMF Gain | 0 | -64..63 | Y | dB = raw·20/63 | ChannelDeviceProtocol.cpp:449-451 | exact port |
| 19 | LMF Q | 64 | 0..127 | Y | Q = 0.7·exp(raw·0.01002335) | ChannelDeviceProtocol.cpp:456-458 | exact port |
| 20 | LMF On | 0 | 0..1 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 21 | LF Gain | 0 | -64..63 | Y | dB = raw·20/63 | ChannelDeviceProtocol.cpp:449-451; DLLChannelEQDSP.cpp:595-638 | exact port |
| 22 | LF Bell | 0 | 0..1 | Y | 0 = shelf, 1 = bell (Q 2.5) | DLLChannelEQDSP.cpp:597-623 | exact port |
| 23 | LF On | 0 | 0..1 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 24 | Bypass Insert FX | 0 | 0..1 | Y | bypasses insert callback | DLLChannelDevice.cpp:1407+ | exact (routing) |
| 25 | To Insert FX Peak Meter | 0 | 0..20 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 26 | From Insert FX Peak Meter | 0 | 0..20 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 27 | Comp On | 0 | 0..1 | Y | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 28 | C Threshold | 64 | 0..127 | Y | dB = raw·52/127 − 52 | ChannelDeviceProtocol.cpp:471-473 | map exact; DSP not modelled |
| 29 | C Release | 64 | 0..127 | Y | s = raw·0.9/127 + 0.1 | ChannelDeviceProtocol.cpp:474-476 | map exact; DSP not modelled |
| 30 | C Ratio | 64 | 0..127 | Y | engine uses raw/127 (GetCompressorRatio = 1/(1−raw/127)² for display) | ChannelDeviceProtocol.cpp:466-470; DLLChannelDevice.cpp:3363 | map exact; DSP not modelled |
| 31 | C Peak | 0 | 0..1 | Y | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 32 | C Fast Atk | 0 | 0..1 | Y | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 33 | Comp Gain Reduction | 0 | 0..5 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 34 | Gate On | 0 | 0..1 | Y | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 35 | G Threshold | 32 | 0..127 | Y | dB = raw·52/127 − 52 | ChannelDeviceProtocol.cpp:480-482 | map exact; DSP not modelled |
| 36 | G Hold | 0 | 0..127 | Y | s = 4·(raw/127)² | ChannelDeviceProtocol.cpp:486-489 | map exact; DSP not modelled |
| 37 | G Release | 64 | 0..127 | Y | s = raw·0.9/127 + 0.1 | ChannelDeviceProtocol.cpp:483-485 | map exact; DSP not modelled |
| 38 | G Range | 64 | 0..127 | Y | dB = −40·raw/127 | ChannelDeviceProtocol.cpp:477-479 | map exact; DSP not modelled |
| 39 | G Fast Atk | 0 | 0..1 | Y | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 40 | Expander | 0 | 0..1 | Y | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 41 | Gate Gain Reduction | 0 | 0..5 | – | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 42 | Key On | 0 | 0..1 | Y | meter/engine-only or switch, not modelled | ChannelDeviceProtocol.cpp:34-234 | n/a |
| 43 | FX1 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 44 | FX1 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 45 | FX1 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 46 | FX2 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 47 | FX2 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 48 | FX2 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 49 | FX3 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 50 | FX3 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 51 | FX3 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 52 | FX4 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 53 | FX4 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 54 | FX4 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 55 | FX5 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 56 | FX5 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 57 | FX5 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 58 | FX6 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 59 | FX6 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 60 | FX6 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 61 | FX7 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 62 | FX7 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 63 | FX7 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 64 | FX8 Send On | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 65 | FX8 Send Level | 63 | 0..127 | Y | send gain = NSMainMixerUtils::ByteParameterToGain(raw) (not ported) | DLLChannelDevice.cpp:2143-2144 | unknown |
| 66 | FX8 Pre Fader | 0 | 0..1 | Y | send switch | DLLChannelDevice.cpp:2809-2818 | not modelled |
| 67–75 | VU / input meters | 0 | meter | – | engine-only meters | ChannelDeviceProtocol.cpp:194-213 | n/a |
| 76 | Tuner Enable | 0 | 0..1 | Y | tuner switch | ChannelDeviceProtocol.cpp:216-217 | n/a |
| 77–83 | tuner readouts / full-scale meters | 0 | meter | – | engine-only | ChannelDeviceProtocol.cpp:218-233 | n/a |

(Indices 67–83 are, in order: VU Meter, VU Meter L/R, Input Meter L/R, Input Peak L/R, Input Clip L/R, Tuner Enable (76), Tuner Pitch Detected, Note Number, Cents, Full-Scale Input Meter L/R, Full-Scale Peak L/R. Exact names are in `CHANNEL_BP`.)

### Long parameters

| idx | name | default | range | saved | physical | source | confidence |
|---|---|---|---|---|---|---|---|
| 0 | LPF Frequency | 673 | 0..1000 | Y | Hz = 100·exp(raw·0.0052983173) (100 Hz–20 kHz); default 673 = 3537 Hz | ChannelDeviceProtocol.cpp:403-405 | exact port |
| 1 | HPF Frequency | 422 | 0..1000 | Y | Hz = 20·exp(raw·0.0052983173) (20 Hz–4 kHz); default 422 = 187 Hz | ChannelDeviceProtocol.cpp:410-412 | exact port |
| 2 | HF Frequency | 500 | 0..1000 | Y | Hz = 1500·exp(raw·0.002685577) (1.5–22 kHz); default 500 = 5745 Hz | ChannelDeviceProtocol.cpp:421-423 | exact port |
| 3 | HMF Frequency | 500 | 0..1000 | Y | Hz = 600·exp(raw·0.002456736) (600 Hz–7 kHz); default 500 = 2049 Hz | ChannelDeviceProtocol.cpp:428-430 | exact port |
| 4 | LMF Frequency | 500 | 0..1000 | Y | Hz = 200·exp(raw·0.002302581) (200 Hz–2 kHz); default 500 = 632 Hz | ChannelDeviceProtocol.cpp:435-437 | exact port |
| 5 | LF Frequency | 500 | 0..1000 | Y | Hz = 40·exp(raw·0.00270805) (40–600 Hz); default 500 = 155 Hz | ChannelDeviceProtocol.cpp:442-444 | exact port |
| 6 | Level | 734 | 0..1000 | Y | gain = (raw/734)³; dB = 60·log10(raw/734); 734 = 0 dB, 1000 = +8.06 dB | AudioVolumeGain.h:35-40; DLLChannelDevice.cpp:2876-2900 | exact port (static; 0.995 one-pole smoothing ignored) |
| 7 | Pan | 0 | -100..100 | Y | p = raw/200+0.5; L = C1p²+C2p+√2, R = same(1−p) | DLLChannelDevice.cpp:2447-2449, 2790-2802 | exact port |
| 8 | PDC Insert FX Return Delay Frames | 0 | 0..480000 | – | engine-only | ChannelDeviceProtocol.cpp:261 | n/a |
| 9 | PDC Send FX Out Delay Frames | 0 | 0..480000 | – | engine-only | ChannelDeviceProtocol.cpp:264 | n/a |
| 10 | Channel Latency Adjust (samples) | 0 | -480000..480000 | Y | samples of PDC offset | ChannelDeviceProtocol.cpp:266 | not modelled |

The source's inverse maps (`*HzToParameterValue`, `EQGaindBToParameterValue`, `GetEQQByteParameterValue`) **truncate**, through `CheckedCast` of a float. The Python `*_to_raw` functions round to the **nearest** raw value instead. Every raw value round-trips, and the tests check this.

### Signal path (static parameters)

Sources: `DLLChannelDevice.cpp:1346-1404` (before the inserts), `:1640-1745` (after the inserts), `:1747-1895` (output), and `:2385-2417` (section setup).

1. **Input.** A Mix Channel is mono when only the L input is connected (`:796-842`). A Disk Channel is mono when both the selected input and the recording are mono (`:1296-1302`). A mono channel processes one buffer and is duplicated L=R after the sections (`:1398-1403`, `:1715-1719`).
2. **Input gain and phase.** The channel is multiplied by `±exp(dB·0.1155)` (`DLLChannelEQDSP.cpp:137-145, 222-251`). Note the constant 0.1155: +18 dB on the knob actually gives +18.06 dB.
3. **Sections** = (Dynamics, EQ) by default, or (EQ, Dynamics) if Dyn Post EQ is on. The EQ section runs **LPF → HPF → LF shelf → HF shelf → HF bell → LF bell → LMF → HMF** (`:1133-1146`, `DLLChannelEQDSP.cpp:253-326, 826-938`). If Filters Dyn S/C is on, the HPF/LPF are applied only to the dynamics key copy, not to the audio. **Dynamics are not modelled.**
4. **Insert FX.** Insert Pre moves step 3 here, after the inserts. In the port, this is the `insert=` callback.
5. **Mute, pan and width** (`:2836-2842`): `outL = LL·inL + RL·inR`, `outR = LR·inL + RR·inR`, where
   - `LL = lp·(0.5+w)`
   - `RL = lp·(0.5−w)`
   - `LR = rp·(0.5−w)`
   - `RR = rp·(0.5+w)`

   and `w = Width/127·0.5`.
6. **Fader** = `(Level/734)³` (`:2876-2900`). The 0.995 one-pole smoothing only matters when the fader moves.

#### Pan law (exact)
- `p = clip(Pan/200 + 0.5, 0, 1)`.
- `leftPan = C1·p² + C2·p + √2` and `rightPan = C1·(1−p)² + C2·(1−p) + √2`, with `C1 = 2√2 − 4` and `C2 = 4 − 3√2`.

The result is a quadratic "3 dB" law:
- Centre: L = R = 1.0 (0 dB).
- Hard left: L = √2 (+3.01 dB), R = 0.
- Pan = ±50: the near side is 1.2803 (+2.15 dB) and the far side 0.5732 (−4.83 dB).

For a **mono** source, Width has no effect and the output is (leftPan, rightPan)·x. For a **stereo** source at Width 127, pan acts as a balance: hard left outputs `√2·L` and drops R. At Width 0, the output is `0.5·(L+R)` panned.

#### EQ coefficient ports (`ChannelDeviceProtocol.cpp:494-542`)
- `dBToGain(dB) = exp(dB·0.1156)`, so an EQ setting of +20 dB actually gives a 20.08 dB peak.
- **Bell** (`CalcParametricConstants`):
  - `g = dBToGain`.
  - Q scaling:
    - E-mode: `Q *= g` if g < 1.
    - G-mode: `Q *= 0.1·g` if g > 1, else `Q *= 0.1`.
  - `w = min(2π f/sr, 3)`.
  - `b = (Q − w/2)/(2Q + w)`.
  - `A = (0.5−b)(g−1)`, `B = 2b`, `C = 2(0.5+b)cos w`.
  - Transfer function: `H(z) = 1 + A(1−z⁻²)/(1 − C z⁻¹ + B z⁻²)`, which peaks at g at the centre frequency.
  - The LF bell uses Q 2.5 and the HF bell uses Q 2.96, both always in E-mode. LMF and HMF use the knob Q and the E-mode switch.
  - A band is bypassed if |A| < 1/20000 (`DLLChannelEQDSP.cpp:599` etc.).
- **LF shelf** (`DLLChannelEQDSP.cpp:613-637`):
  - `fr = −40·f/sr`, `k = dB·fr·0.915/20`.
  - If k > 0: `(A, B) = (e^{fr+k}, e^{fr})`. Otherwise: `(A, B) = (e^{fr}, e^{fr−k})`.
  - `H = (1 − A z⁻¹)/(1 − B z⁻¹)`.
  - Measured on the port at 48 kHz, with the knob at +20 dB and 155 Hz: +20.8 dB at 10 Hz and +14.6 dB at 155 Hz.
- **HF shelf** (`:658-690`):
  - `fr = e^{−6 f/sr}`, `inv = 1−fr`, `k = dB·0.9/20`, `add = inv·k`.
  - If k > 0: `A = fr+add`, `B = fr`, `C = inv/(1−A)`. Otherwise: `A = fr`, `B = fr−add`, `C = (1−B)/inv`.
  - `H = C(1 − A z⁻¹)/(1 − B z⁻¹)`.
  - With the knob at +20 dB and 5.7 kHz: +19.9 dB at 5.7 kHz and +22.3 dB at 20 kHz.
- **LPF**, 12 dB/oct (`:522-532`):
  - `q = 1.56`, `w = min(2πf/sr, π)`; if w > 1.2, `q *= 1 + 0.54(w−1.2)`.
  - `fb2 = (q−w)/(q+w)`, `fb1 = (1+fb2)cos w`, `g = (1−fb1+fb2)/2`.
  - `H = g(1+z⁻¹)/(1 − fb1 z⁻¹ + fb2 z⁻²)`.
- **HPF**, 18 dB/oct (`:534-542`, `DLLChannelEQDSP.cpp:210-220`):
  - `q = 2`, `w = min(2πf/sr, 1.57)`.
  - `fb2 = (q−w)/(q+w)`, `fb1 = (1+fb2)cos w`, `fb3 = 1−w`.
  - `H = (1−z⁻¹)³ / [(1 − fb1 z⁻¹ + fb2 z⁻²)(1 − fb3 z⁻¹)]`.
  - There is no gain normalisation, so the passband is about +0.2 dB. At the default 187 Hz the response is −2.85 dB.
- **E-mode versus G-mode**, LMF at default Q and 632 Hz, measured as half-gain bandwidth on the port:
  - At ±6.4 dB: G = 4.8 octaves, E = 1.5 octaves.
  - At ±20 dB: both are about 2.9 octaves.
- The UI curve (`App/UI/GraphicEQAndSpectrumAnalyzer/SpectrumEQGridView.cpp:48-125, 240-350`) evaluates the same formulas. That is a cross-check of these transfer functions.

API:
- `channel_filter_sections(params, sr)`: (name, b, a) in render order.
- `channel_eq_response(params, freqs, sr)`: dB magnitude.
- `channel_process(x, sr, params, insert=None)`.
- `fit_channel_eq(target_db, freqs, sr, …)`: continuous least squares (gating off, float64), then rounding, then an integer coordinate-descent polish on the exact float32 response. It tries LF/HF bell-versus-shelf, E/G mode, and HPF on/off. It returns `params`, `residual_db`, `rms_db` and `max_abs_db`. On a synthetic target it recovers the exact raw values (residual 0 dB).

## 2. MClass Stereo Imager (`MClass Stereo StereoImager`)

`Devices/MClass/MStereoImager/`. Byte ids: `kBPEffectMode` = 0 (`Rack/Device/Base/EffectProtocol.h:14`), then the device enum starting at 1 (`MStereoImagerProtocol.h:55`).

| idx | name | default | range | stored in | physical | source | confidence |
|---|---|---|---|---|---|---|---|
| 0 | Enabled | 1 | 0..2 (off/on/bypass) | device PARM | effect mode | MStereoImagerProtocol.cpp:24 | exact |
| 1 | Low Width | 0 | -64..63 | PTCH/PARM | w = raw/64 | :27; DLLMStereoImager.cpp:160 | exact port |
| 2 | High Width | 0 | -64..63 | PTCH/PARM | w = raw/64 | :29; DLLMStereoImager.cpp:161 | exact port |
| 3 | X-Over Frequency | 64 | 0..127 | PTCH/PARM | Hz = 100·exp(raw·0.032239): 100 Hz … 6.0 kHz, 64 = 787 Hz | :31, :60-62 | exact port |
| 4 | Solo Mode | 0 | 0..2 | PTCH/PARM | normal / low only / high only | :33; DLLMStereoImager.cpp:291-307 | exact port |
| 5 | Separate Out Mode | 0 | 0..1 | device PARM | separate out carries 0 = low band, 1 = high band | :36; DLLMStereoImager.cpp:308-315 | exact port |
| 6-8 | meters | – | – | not saved | – | :39-44 | n/a |

**DSP** (`DLLMStereoImager.cpp:153-158, 204-316`), exact port:
- **Crossover.**
  - `tw = 2π f/sr`, `q = 0.735603`.
  - `a = 2cos(tw)·e^{−tw/(2q)}`, `b = e^{−tw/q}`, `g = 1 − a + b`.
  - LP: `y = g x + a y₁ − b y₂`, which has unity gain at DC. HP = x − LP, so the bands sum back to the input exactly.
- **Per-band width.**
  - Width > 0: `L' = L(1−w/2) + (L−R)w` and `R' = R(1−w/2) − (L−R)w`. Mid gain is 1−w/2 and side gain is 1+1.5w.
  - Width ≤ 0: `L' = L(1+w/2) + R(−w/2)`, and symmetrically for R. Mid gain is 1 and side gain is 1+w, so −64 collapses the band to mono.
- If only the L input is connected, the device uses R = L (`:405-411`).
- Separate Out: the source writes the **low** band when `fSeperateOutMode == kSeparateOutModeLowBand` (0). Otherwise it writes the high band. The default (0) is therefore the low band.

API: `imager_xover_hz`, `imager_xover_to_raw`, `imager_width_factor`, `imager_width_to_raw`, `imager_band_matrix`, `imager_mid_side_gains`, and `imager_process(x, sr, low_width_raw, high_width_raw, xover_raw, solo_mode)`.

## 3. MClass Maximizer (`MClass Maximizer`)

`Devices/MClass/MMaximizer/`. Ids: Enabled = 0, then `kBPInputGain` = 1 … (`MMaximizerProtocol.h:66-80`).

| idx | name | default | range | stored in | physical | source | confidence |
|---|---|---|---|---|---|---|---|
| 0 | Enabled | 1 | 0..2 | device PARM | effect mode | MMaximizerProtocol.cpp:25 | exact |
| 1 | Input Gain | 64 | 0..127 | PTCH/PARM | dB = raw·12/64 − 12 (−12 … +11.81); linear = exp(dB·0.1156) | :28, :66-68; DLLMMaximizer.cpp:193 | exact port |
| 2 | Limiter Enable | 1 | 0..1 | PTCH/PARM | limiter on | :30; DLLMMaximizer.cpp:209 | exact port |
| 3 | Look Ahead Enable | 0 | 0..1 | PTCH/PARM | on: delay = int(0.004·sr) (+1) samples, threshold 0.93; off: threshold 0.8 | :32; DLLMMaximizer.cpp:198-206 | exact port |
| 4 | Attack Speed | 0 | 0..2 | PTCH/PARM | 4 / 16 / 64 ms; coef = 1 − exp(−4.6/(t·sr)) | :34, :91-108; DLLMMaximizer.cpp:174-175 | exact port |
| 5 | Release Speed | 0 | 0..2 | PTCH/PARM | 150 / 400 / 150 ms + adaptive (Auto) | :36, :72-89; DLLMMaximizer.cpp:178-189, 250-257 | exact port |
| 6 | Output Gain | 64 | 0..127 | PTCH/PARM | dB = raw·12/64 − 12; linear = exp(dB·0.1156)·0.99 | :38, :69-71; DLLMMaximizer.cpp:194-196 | exact port |
| 7 | Soft Clip Enable | 0 | 0..1 | PTCH/PARM | soft clipper after limiter | :40; DLLMMaximizer.cpp:402-436 | exact port |
| 8 | Soft Clip Amount | 64 | 0..127 | PTCH/PARM | a = raw/127·0.65 + 0.01 | :42; DLLMMaximizer.cpp:211-214 | exact port |
| 9 | Output Level Meter Mode | 0 | 0..1 | PTCH/PARM | meter only | :44 | n/a |
| 10-13 | meters | – | – | not saved | – | :47-54 | n/a |

**DSP** (`DLLMMaximizer.cpp:233-436`), ported op for op in `maximizer_process`:
- Order: input gain, then limiter, then soft clip.
- **L and R are limited independently.** Each render unit has its own `VDelays`, so the limiter is not stereo-linked.
- Peak follower: instant attack, release coefficient `1−exp(−4.6/(0.08·sr))`. With look-ahead on, the key is `max(|x|, |delayed mid-tap|)`.
- Envelope target: 1 if `peak < thr`, otherwise `thr/peak`. The envelope moves with the release coefficient when rising and the attack coefficient when falling.
- Output: `y = delayed_x · env · outGain`.
- Auto release adds `clip((ad2·(1−e^{−10/sr})² − 0.4)·0.4, 0, 0.3)` seconds. This is computed once per 64-sample batch.
- Soft clip: `y = (f(x+a) − f(x−a))·0.49/a`, where `f(u) = |u|+1.5` for |u| > 1 and `2 + u²/2` otherwise.
- Initial state comes from `VDelays::Clear()`: peak 0, env 1.

**Fidelity:** exact for static parameters. The 64-sample smoothing of gain changes is ignored. The port is a pure-Python sample loop, so it is slow.

**Quirk:** with the limiter **off**, Output Gain is not applied, because it is only multiplied inside the limiter loop.

## Open questions
- None of this has been verified against audio rendered by Reason. Bounce tests should check:
  - fader 0 dB / pan law;
  - EQ curves, by pink noise through the EQ;
  - the HPF passband gain of +0.2 dB;
  - mono versus stereo Disk Channel behaviour.
- The channel dynamics (`DLLChannelDynamicsDSP.cpp`) and the send-level map (`NSMainMixerUtils::ByteParameterToGain`) are not ported.
- The master section and its fader are not covered.
- Per-batch parameter ramps and the level smoothing are ignored. This only matters for automation.
- `CPropertyDef` ids are assumed to equal the enum position for PTCH/PARM as well. The writer is the same (`WriteParametersOfType`), but that has not been confirmed on a saved MClass device.
- Whether a mono audio-track recording makes the Disk Channel take the "mono" path (`fAudioIsStereo`) still needs to be checked on a real song.
