import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mixlab import reason_devices as rd  # noqa: E402

SR = 48000.0


class TestParmEncoding(unittest.TestCase):
    def test_signed_byte(self):
        self.assertEqual(rd.encode_byte_value(-64), 0xC0)
        self.assertEqual(rd.decode_byte_value(0xC0), -64)
        for v in range(-128, 128):
            self.assertEqual(rd.decode_byte_value(rd.encode_byte_value(v)), v)

    def test_signed_long(self):
        self.assertEqual(rd.encode_long_value(-100), 0xFFFFFF9C)
        self.assertEqual(rd.decode_long_value(0xFFFFFF9C), -100)

    def test_parm_roundtrip(self):
        bp, lp = rd.channel_default_parm_values()
        bp[rd.CHANNEL_BP["Input Gain"]] = -30
        lp[rd.CHANNEL_LP["Pan"]] = -42
        blob = rd.build_parm(rd.CHANNEL_PARM_VERSION, bp, lp)
        ver, bp2, lp2 = rd.parse_parm(blob)
        self.assertEqual(ver, rd.CHANNEL_PARM_VERSION)
        self.assertEqual(bp2, bp)
        self.assertEqual(lp2, lp)


class TestChannelDefaults(unittest.TestCase):
    def test_observed_long_defaults(self):
        _, lp = rd.channel_default_parm_values()
        self.assertEqual(lp, {0: 0x2A1, 1: 0x1A6, 2: 500, 3: 500, 4: 500, 5: 500, 6: 0x2DE, 7: 0, 10: 0})

    def test_observed_byte_defaults(self):
        bp, _ = rd.channel_default_parm_values()
        self.assertEqual(bp[0], 0x7F)  # width
        self.assertEqual(bp[rd.CHANNEL_BP["HMF Q"]], 0x40)
        self.assertEqual(bp[rd.CHANNEL_BP["LMF Q"]], 0x40)
        self.assertEqual(bp[rd.CHANNEL_BP["Input Gain"]], 0)
        self.assertNotIn(rd.CHANNEL_BP["HF On"], bp)  # not saved
        self.assertEqual(rd.CHANNEL_BP["Full Scale Input Right Peak"], 83)
        self.assertEqual(rd.CHANNEL_BP["Bypass Insert FX"], 24)

    def test_default_physical(self):
        self.assertAlmostEqual(rd.fader_gain(734), 1.0, places=6)
        self.assertEqual(rd.pan_gains(0), (1.0, 1.0))
        self.assertAlmostEqual(rd.channel_freq_hz("HPF", 422), 187.1, delta=0.2)


class TestChannelMaps(unittest.TestCase):
    def test_input_gain(self):
        self.assertAlmostEqual(rd.input_gain_db(63), 18.0, places=5)
        self.assertAlmostEqual(rd.input_gain_db(-64), -18.2857, places=3)
        for r in range(-64, 64):
            self.assertEqual(rd.input_gain_db_to_raw(rd.input_gain_db(r)), r)
        self.assertLess(rd.input_gain_linear(0, invert=True), 0)

    def test_eq_gain_and_q(self):
        self.assertAlmostEqual(rd.eq_gain_db(63), 20.0, places=5)
        for r in range(-64, 64):
            self.assertEqual(rd.eq_gain_db_to_raw(rd.eq_gain_db(r)), r)
        self.assertAlmostEqual(rd.eq_q(0), 0.7, places=6)
        self.assertAlmostEqual(rd.eq_q(127), 2.5, delta=0.01)
        for r in range(0, 128):
            self.assertEqual(rd.eq_q_to_raw(rd.eq_q(r)), r)

    def test_freq_ranges_roundtrip(self):
        expect = {"LPF": (100, 20000), "HPF": (20, 4000), "HF": (1500, 22000), "HMF": (600, 7000),
                  "LMF": (200, 2000), "LF": (40, 600)}
        for band, (lo, hi) in expect.items():
            self.assertAlmostEqual(rd.channel_freq_hz(band, 0), lo, delta=lo * 1e-4)
            self.assertAlmostEqual(rd.channel_freq_hz(band, 1000), hi, delta=hi * 0.01)
            for r in range(0, 1001, 7):
                self.assertEqual(rd.channel_freq_to_raw(band, rd.channel_freq_hz(band, r)), r)

    def test_fader(self):
        self.assertAlmostEqual(rd.fader_db(1000), 8.058, places=2)
        for r in range(1, 1001):
            self.assertEqual(rd.fader_db_to_raw(rd.fader_db(r)), r)
        self.assertEqual(rd.fader_gain(0), 0.0)

    def test_pan_law(self):
        l, r = rd.pan_gains(-100)
        self.assertAlmostEqual(l, math.sqrt(2), places=5)
        self.assertAlmostEqual(r, 0.0, places=6)
        l, r = rd.pan_gains(100)
        self.assertAlmostEqual(l, 0.0, places=6)
        self.assertAlmostEqual(r, math.sqrt(2), places=5)
        for p in range(-100, 101):
            self.assertEqual(rd.pan_to_raw(p / 100.0), p)

    def test_width_matrix(self):
        np.testing.assert_allclose(rd.pan_width_matrix(0, 127), np.eye(2), atol=1e-7)
        np.testing.assert_allclose(rd.pan_width_matrix(0, 0), np.full((2, 2), 0.5), atol=1e-7)


class TestChannelDSP(unittest.TestCase):
    f = np.geomspace(20, 20000, 200)

    def test_flat_when_gains_zero(self):
        p = {"EQ On": 1}
        db = rd.channel_eq_response(p, self.f, SR)
        np.testing.assert_allclose(db, 0.0, atol=1e-5)
        for em in (0, 1):
            for lb in (0, 1):
                for hb in (0, 1):
                    db = rd.channel_eq_response({"EQ On": 1, "EQ E Mode": em, "LF Bell": lb, "HF Bell": hb},
                                                self.f, SR)
                    np.testing.assert_allclose(db, 0.0, atol=1e-5)

    def test_eq_off_flat(self):
        db = rd.channel_eq_response({"EQ On": 0, "LMF Gain": 40, "HF Gain": -30}, self.f, SR)
        np.testing.assert_allclose(db, 0.0, atol=1e-5)

    def test_bell_peak_gain(self):
        # Peak of the parametric section equals exp(dB*0.1156) at its centre.
        for em in (0, 1):
            p = {"EQ On": 1, "EQ E Mode": em, "LMF Gain": 63, "LMF Frequency": 500}
            fc = rd.channel_freq_hz("LMF", 500)
            db = rd.channel_eq_response(p, [fc], SR)[0]
            self.assertAlmostEqual(db, 20 * math.log10(math.exp(20 * 0.1156)), delta=0.05)

    def test_shelves_direction(self):
        lo = rd.channel_eq_response({"EQ On": 1, "LF Gain": 40}, [30.0, 10000.0], SR)
        self.assertGreater(lo[0], 5)
        self.assertLess(abs(lo[1]), 0.5)
        hi = rd.channel_eq_response({"EQ On": 1, "HF Gain": -40}, [100.0, 18000.0], SR)
        self.assertLess(hi[1], -5)
        self.assertLess(abs(hi[0]), 0.5)

    def test_hpf(self):
        db = rd.channel_eq_response({"HPF On": 1, "HPF Frequency": 422}, [20.0, 2000.0], SR)
        self.assertLess(db[0], -20)
        self.assertLess(abs(db[1]), 1.0)

    def test_process_matches_response(self):
        p = {"EQ On": 1, "LMF Gain": 30, "HMF Gain": -20, "LF Gain": 10, "HF Gain": 15, "HF Bell": 1,
             "HPF On": 1}
        imp = np.zeros(1 << 15)
        imp[0] = 1
        y = rd.channel_process(imp, SR, p)
        self.assertEqual(y.shape, (len(imp), 2))
        np.testing.assert_allclose(y[:, 0], y[:, 1])  # mono centred -> L == R, unity pan at centre
        H = np.fft.rfft(y[:, 0])
        fr = np.fft.rfftfreq(len(imp), 1 / SR)
        sel = (fr > 50) & (fr < 15000)
        want = rd.channel_eq_response(p, fr[sel], SR)
        np.testing.assert_allclose(20 * np.log10(np.abs(H[sel])), want, atol=1e-3)

    def test_process_gains_and_pan(self):
        x = np.ones(1000)
        y = rd.channel_process(x, SR, {"Input Gain": 63, "Level": 1000, "Pan": -100, "Invert Phase": 1})
        g = rd.input_gain_linear(63, True) * rd.fader_gain(1000)
        self.assertAlmostEqual(y[-1, 0], g * math.sqrt(2), places=4)
        self.assertAlmostEqual(y[-1, 1], 0.0, places=6)

    def test_stereo_width_zero_is_mono(self):
        x = np.stack([np.ones(10), -np.ones(10)], axis=1)
        y = rd.channel_process(x, SR, {"Width": 0})
        np.testing.assert_allclose(y, 0.0, atol=1e-7)

    def test_fit_recovers(self):
        f = np.geomspace(30, 18000, 120)
        truth = {"EQ On": 1, "LF Gain": 12, "LF Frequency": 400, "LMF Gain": -15, "LMF Frequency": 450,
                 "LMF Q": 70, "HMF Gain": 10, "HMF Frequency": 600, "HMF Q": 50, "HF Gain": 9,
                 "HF Frequency": 450}
        tgt = rd.channel_eq_response(truth, f, SR)
        r = rd.fit_channel_eq(tgt, f, SR, fit_hpf=False, emode=False, shelf_modes=[(False, False)])
        self.assertLess(r["rms_db"], 0.3)
        self.assertEqual(set(r["params"]) >= {"LF Gain", "HMF Q", "HF Frequency"}, True)


class TestImager(unittest.TestCase):
    def test_maps(self):
        self.assertAlmostEqual(rd.imager_xover_hz(0), 100.0, places=3)
        self.assertAlmostEqual(rd.imager_xover_hz(127), 6000.0, delta=10)
        for r in range(128):
            self.assertEqual(rd.imager_xover_to_raw(rd.imager_xover_hz(r)), r)
        for r in range(-64, 64):
            self.assertEqual(rd.imager_width_to_raw(rd.imager_width_factor(r)), r)
        self.assertEqual(rd.imager_mid_side_gains(-64), (1.0, 0.0))
        self.assertEqual(rd.imager_mid_side_gains(0), (1.0, 1.0))

    def test_neutral_is_identity(self):
        rng = np.random.default_rng(1)
        x = rng.standard_normal((4000, 2))
        y = rd.imager_process(x, SR, 0, 0, 64)
        np.testing.assert_allclose(y, x, atol=1e-6)

    def test_low_band_mono(self):
        n = 1 << 14
        t = np.arange(n) / SR
        side = np.sin(2 * np.pi * 50 * t)
        x = np.stack([side, -side], axis=1)
        y = rd.imager_process(x, SR, -64, 0, 64)
        self.assertLess(np.max(np.abs(y[n // 2:])), 0.1)


class TestMaximizer(unittest.TestCase):
    def test_maps(self):
        self.assertAlmostEqual(rd.maximizer_gain_db(64), 0.0)
        self.assertAlmostEqual(rd.maximizer_gain_db(0), -12.0)
        for r in range(128):
            self.assertEqual(rd.maximizer_gain_db_to_raw(rd.maximizer_gain_db(r)), r)
        self.assertEqual(rd.maximizer_lookahead_samples(48000, True), 192)
        self.assertEqual(rd.MAXIMIZER_ATTACK_MS, {0: 4, 1: 16, 2: 64})

    def test_limits(self):
        n = 48000
        t = np.arange(n) / SR
        x = 2.0 * np.sin(2 * np.pi * 200 * t)
        y = rd.maximizer_process(x, SR, lookahead_on=1)
        self.assertLess(np.max(np.abs(y[n // 2:])), 1.0)
        self.assertGreater(np.max(np.abs(y[n // 2:])), 0.8)

    def test_passthrough_below_threshold(self):
        x = 0.1 * np.ones(2000)
        y = rd.maximizer_process(x, SR)
        self.assertAlmostEqual(y[-1], 0.1 * rd.maximizer_output_gain_linear(64), places=6)


if __name__ == "__main__":
    unittest.main()
