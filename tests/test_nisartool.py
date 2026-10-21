# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pyproj", "matplotlib", "requests"]
# ///
"""Offline NISAR regressions. Run: uv run tests/test_nisartool.py

Synthetic grids and RFI metadata replace remote reads; no credentials or downloads are used.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
from pyproj import CRS, Geod, Transformer


_runtime = tempfile.TemporaryDirectory(prefix='nisartool-tests-')
with mock.patch.dict(os.environ, {'NISARTOOL_CACHE': _runtime.name, 'MPLCONFIGDIR': _runtime.name}):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    script = Path(__file__).resolve().parents[1] / 'skills/sentinel-aircraft-ships/scripts/nisartool.py'
    spec = importlib.util.spec_from_file_location('nisartool', script)
    nt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nt)

GRANULE = 'NISAR_L2_PR_GCOV_010_005_A_018_4005_DHDH_A_20260109T125114_20260109T125149_P05023_N_F_J_001'


class NisarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(plt.close, 'all')

    def run_blobs(self, lat, lon, epsg, sources):
        local = CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84')
        x0, y0 = Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)
        x = x0 + np.arange(-100000, 100001, 1000.)
        y = y0 + np.arange(100000, -100001, -1000.)
        east, north = Transformer.from_crs(epsg, local, always_xy=True).transform(*np.meshgrid(x, y))
        power = np.ones(east.shape)
        for e, n, strength, sigma in sources:
            distance2 = (east - e) ** 2 + (north - n) ** 2
            power += np.where(distance2 < (3 * sigma) ** 2, strength * np.exp(-distance2 / (2 * sigma ** 2)), 0)
        args = SimpleNamespace(lat=lat, lon=lon, granule=GRANULE, size_km=120., pol='HV', min_db=6., max=1,
                               out=str(Path(self.tmp.name) / 'blobs.png'))
        with mock.patch.object(nt, 'read_window', return_value=({'HV': power}, x, y, epsg, 1000.)), \
                contextlib.redirect_stdout(io.StringIO()):
            nt.cmd_blobs(args)
        result = json.loads(Path(args.out).with_suffix('.json').read_text())
        self.assertGreater(Path(args.out).stat().st_size, 1000)
        return result, local, plt.gcf()

    def test_outside_source_cannot_displace_inside_source(self):
        for lat, lon, epsg in ((80., 0., 3413), (-80., 45., 3031), (34., -117., 32611)):
            with self.subTest(epsg=epsg):
                inside = (15000., -15000., 1000., 3000.)
                result, local, fig = self.run_blobs(lat, lon, epsg, [inside, (75000., 0., 1e8, 3000.)])
                self.assertEqual(len(result['blobs']), 1)
                blob = result['blobs'][0]
                e, n = Transformer.from_crs(4326, local, always_xy=True).transform(blob['peak_lon'], blob['peak_lat'])
                self.assertLess(abs(e - inside[0]), 2000)
                self.assertLess(abs(n - inside[1]), 2000)
                self.assertFalse(blob['runs_off_data'])
                # Both image panels mask excluded pixels instead of showing extra search area.
                for ax in fig.axes:
                    self.assertGreater(np.ma.getmaskarray(ax.images[0].get_array()).sum(), 0)
                clean, _, _ = self.run_blobs(lat, lon, epsg, [inside])
                self.assertEqual(result['blobs'], clean['blobs'])
                plt.close('all')

    def test_only_outside_source_yields_no_blob(self):
        result, _, _ = self.run_blobs(80., 0., 3413, [(75000., 0., 1e8, 3000.)])
        self.assertEqual(result['blobs'], [])

    def test_search_boundary_marks_blob_as_truncated(self):
        result, _, _ = self.run_blobs(80., 0., 3413, [(58000., 0., 1000., 5000.)])
        self.assertEqual(len(result['blobs']), 1)
        self.assertTrue(result['blobs'][0]['runs_off_data'])

    def rfi_chunk(self, all_excluded=False, with_level=True):
        hc = np.zeros((32, 24, 64))
        if all_excluded:
            hc[:] = .1
        else:
            hc[12:20, 8:16, :] = .2
        chunk = {'frequency': np.linspace(1219e6, 1259e6, 64), 'nativeDopplerTime': np.arange(32.),
                 'slantRange': 800000 + np.arange(24.) * 1000, 'hitCount': hc}
        if with_level:
            level = np.ones((32, 24))
            level[8:24, 6:18] = 1000
            chunk['signalMean'] = level
        return chunk

    def run_rfi(self, chunks):
        fake = {f'/science/LSAR/RSLC/metadata/RFI/frequency{fr}/HV': {'raw1': chunk}
                for fr, chunk in chunks.items()}
        args = SimpleNamespace(granule=GRANULE, pol=None, height=0., tone_frac=.05,
                               out=str(Path(self.tmp.name) / 'rfi.png'))
        stdout = io.StringIO()
        with mock.patch.object(nt, 'h5', return_value=contextlib.nullcontext(fake)), \
                mock.patch.object(nt, 'locate', return_value='unused'), \
                mock.patch.object(nt, 'squint_locator', return_value=lambda t, r: (32., -110.)), \
                contextlib.redirect_stdout(stdout), np.errstate(divide='raise', invalid='raise'):
            nt.cmd_rfimeta(args)
        text = Path(args.out).with_suffix('.json').read_text()
        self.assertNotIn('NaN', text)
        self.assertGreater(Path(args.out).stat().st_size, 1000)
        return json.loads(text)['freqs'], stdout.getvalue(), plt.gcf()

    def test_all_excluded_preserves_levels_and_other_frequency(self):
        result, stdout, fig = self.run_rfi({'A': self.rfi_chunk(True), 'B': self.rfi_chunk()})
        self.assertEqual(result['A']['flag_status'], 'no_frequency_bins')
        self.assertEqual(result['A']['frequency_bins_used'], 0)
        self.assertEqual(result['A']['flag_hotspots'], [])
        self.assertTrue(result['A']['level_hotspots'])
        self.assertEqual(result['B']['flag_status'], 'ok')
        self.assertEqual(result['B']['frequency_bins_used'], 64)
        self.assertTrue(result['B']['flag_hotspots'])
        self.assertIn('no frequency bins', stdout)
        self.assertTrue(any('no frequency bins' in t.get_text() for ax in fig.axes for t in ax.texts))
        self.assertEqual(len(fig.axes[0].collections), 0)  # unavailable flag map is not plotted
        self.assertTrue(fig.axes[1].collections)  # signalMean still plotted
        self.assertTrue(fig.axes[2].lines)  # full spectrum still plotted

    def test_all_excluded_without_signal_mean(self):
        result, _, _ = self.run_rfi({'A': self.rfi_chunk(True, with_level=False)})
        self.assertEqual(result['A']['flag_status'], 'no_frequency_bins')
        self.assertEqual(result['A']['level_hotspots'], [])

    def test_partial_tone_exclusion_retains_flag_detection(self):
        chunk = self.rfi_chunk()
        chunk['hitCount'][:, :, 0] = 1.
        result, _, _ = self.run_rfi({'A': chunk})
        self.assertEqual(result['A']['flag_status'], 'ok')
        self.assertEqual(result['A']['frequency_bins_used'], 59)
        self.assertEqual(result['A']['frequency_bins_total'], 64)
        self.assertEqual(result['A']['tone_frac'], .05)
        self.assertTrue(result['A']['flag_hotspots'])

    def test_quiet_record_is_distinct_from_unavailable_analysis(self):
        chunk = self.rfi_chunk(with_level=False)
        chunk['hitCount'][:] = 0
        result, _, _ = self.run_rfi({'A': chunk})
        self.assertEqual(result['A']['flag_status'], 'ok')
        self.assertEqual(result['A']['frequency_bins_used'], 64)
        self.assertEqual(result['A']['flag_hotspots'], [])

    def test_release_follows_crid_number_not_letter(self):
        name = lambda crid: GRANULE.replace('P05023', crid)
        self.assertEqual(nt.parse_name(name('X05010'))['release'], 'beta')
        self.assertEqual(nt.parse_name(name('P05023'))['release'], 'provisional')
        self.assertEqual(nt.parse_name(name('X05026'))['release'], 'provisional')

    def test_scenes_keeps_latest_product_counter(self):
        old, new = GRANULE, GRANULE[:-3] + '002'
        args = SimpleNamespace(lat=54.7, lon=20.5, start='2026-01-01', end='2026-01-31', release='any', all_modes=False)
        out = io.StringIO()
        with mock.patch.object(nt, 'cmr', return_value=[{'title': new}, {'title': old}]), contextlib.redirect_stdout(out):
            nt.cmd_scenes(args)
        self.assertIn(new, out.getvalue())
        self.assertNotIn(old + '\n', out.getvalue())
        self.assertIn('1 pass(es)', out.getvalue())

    def test_scenes_counts_adjacent_frames_as_one_pass(self):
        f18 = 'NISAR_L2_PR_GCOV_022_034_A_018_4005_DHDH_A_20260604T130753_20260604T130828_P05023_N_F_J_001'
        f19 = 'NISAR_L2_PR_GCOV_022_034_A_019_4005_DHDH_A_20260604T130827_20260604T130902_P05023_N_F_J_001'
        nxt = 'NISAR_L2_PR_GCOV_023_034_A_018_4005_DHDH_A_20260616T130752_20260616T130827_P05023_N_F_J_001'
        args = SimpleNamespace(lat=33.81, lon=-117.92, start='2026-06-01', end='2026-06-30', release='any', all_modes=False)
        out = io.StringIO()
        with mock.patch.object(nt, 'cmr', return_value=[{'title': f19}, {'title': nxt}, {'title': f18}]), contextlib.redirect_stdout(out):
            nt.cmd_scenes(args)
        self.assertIn('2 pass(es)', out.getvalue())
        self.assertIn(f'same pass (frames overlap; use either): {f19}', out.getvalue())

    def test_pooled_beam_shape_recovers_hump_vertex(self):
        rng = np.random.default_rng(3)
        pool = []
        for _ in range(150):  # each pulse sees 17 km of the hump, with its own gain
            r = 940e3 + rng.uniform(-25e3, 8e3) + np.arange(0, 17e3, 150.)
            pool.append((r, rng.normal(0, 3) - (r - 945e3) ** 2 / (2 * 20e3 ** 2) + rng.normal(0, .05, r.size), 0.))
        self.assertLess(abs(nt._pooled(pool)[0] - 945e3), 300)

    def _synthetic_pass(self, lat, lon, h):
        """Circular orbit with its ground track ~270 km east of the point, moving north: orbit metadata,
        and the true beam-crossing time and slant range of the point."""
        e = nt.ecef(lat, lon, h)
        u = nt.ecef(lat, lon + 3.0, 0.); u /= np.linalg.norm(u)
        n = np.cross(np.cross(u, [0, 0, 1.]), u); n /= np.linalg.norm(n)
        rs, w = 6378e3 + 747e3, 7480. / (6378e3 + 747e3)
        t = np.arange(-200., 200., 1.)
        m = {'ot': t, 'opos': rs * (np.cos(w * t)[:, None] * u + np.sin(w * t)[:, None] * n),
             'ovel': rs * w * (-np.sin(w * t)[:, None] * u + np.cos(w * t)[:, None] * n), 'fc': 1239e6, 'left': True}
        o = nt.RawOrbit(m)
        tt = np.linspace(-60, 60, 120001)
        _, rd = o.rdot(tt, e)
        tb = float(tt[np.argmin(np.abs(-2 * m['fc'] * rd / nt.LIGHT - nt.FDC_NOMINAL))])
        return m, tb, float(np.linalg.norm(o.p(tb) - e))

    def test_raw_fix_recovers_emitter_left_of_track(self):
        lat, lon, h = 35.08, -117.58, 800.
        m, tb, R = self._synthetic_pass(lat, lon, h)
        cues = {'R': R, 'Rse': 300., 'tR': tb, 'tb': tb, 'tbse': .15}
        with mock.patch.object(nt, 'dem_height', return_value=h):
            fix = nt.raw_fix([nt.RawPass(m, cues, None, 0.)])
        _, _, d = Geod(ellps='WGS84').inv(lon, lat, fix['lon'], fix['lat'])
        self.assertLess(d, 30)
        self.assertTrue(fix['height_converged'])

    def test_raw_pulses_finds_long_pulse_and_clipping(self):
        rng = np.random.default_rng(5)
        fs, nl, nc = 24e6, 600, 8192  # 341 us lines: a pulse fills a minority of each, as in real windows
        lut = np.arange(4096) - 2047.5  # code -> value
        sig = rng.normal(0, 3, (nl, nc)) + 1j * rng.normal(0, 3, (nl, nc))
        k = np.arange(nc) / fs
        tp = int(88e-6 * fs)
        for i in range(nl):
            if i < 80 or 200 <= i < 240:  # complete 88 us chirps at +1 MHz, 12 kHz/us; lines 200-239 saturate
                s0 = 500 + (i * 37) % 4000
                tt = k[:tp]
                amp = 5000. if i >= 200 else 200.
                sig[i, s0:s0 + tp] += amp * np.exp(2j * np.pi * (1e6 * tt + 6e9 * (tt - tt.mean()) ** 2))
            elif i < 500:  # more numerous 12 us fragments of the same emitter
                s0 = 300 + (i * 53) % 7000
                sig[i, s0:s0 + int(12e-6 * fs)] += 200. * np.exp(2j * np.pi * 1e6 * k[:int(12e-6 * fs)])
        code = lambda v: np.clip(np.round(v + 2047.5), 0, 4095).astype(np.uint16)
        raw = np.stack([code(sig.real), code(sig.imag)], -1)
        m = {'utc': np.arange(nl) * 5e-4, 'sr': 900e3 + np.arange(nc) * nt.LIGHT / (2 * fs)}
        p, _ = nt.raw_pulses(raw, lut, m, 0, 0, 1e6, 1.5e6)
        self.assertAlmostEqual(p['tp_us'], 88, delta=3)
        self.assertGreaterEqual(len(p['line']), 100)
        clipped = set(p['line'][p['clipped']])
        self.assertTrue(clipped and clipped <= set(range(200, 240)))
        self.assertLess(np.median(p['phrms'][p['line'] < 80]), .6)  # linear chirps pass the chirp-fit test

    def test_joint_fix_weights_each_pass_by_its_own_spread(self):
        lat, lon, h = 35.08, -117.58, 800.
        m, tb, R = self._synthetic_pass(lat, lon, h)
        good = nt.RawPass(m, {'R': R, 'Rse': 300., 'tR': tb, 'tb': tb, 'tbse': .15}, None, 0.)
        bad = nt.RawPass(m, {'R': R + 20e3, 'Rse': 100e3, 'tR': tb, 'tb': tb, 'tbse': .15}, None, 0.)
        with mock.patch.object(nt, 'dem_height', return_value=h):
            fix = nt.raw_fix([good, bad])
        _, _, d = Geod(ellps='WGS84').inv(lon, lat, fix['lon'], fix['lat'])
        self.assertLess(d, 200)  # equal weights would put it ~10 km off

    def test_raw_window_drops_completion_log_of_a_deleted_file(self):
        key = nt.cache_path('rawwin|g|p|0|1024|0|8')[:-4] + '.npy'
        log = key[:-4] + '.blocks'
        with open(log, 'w') as fh:
            fh.write('0 512 ')
        with mock.patch.object(nt, 'raw_blocks') as rb, contextlib.redirect_stdout(io.StringIO()):
            raw = nt.raw_window('g', 'u', {'path': 'p'}, 0, 1024, 0, 8, 1)
        self.assertEqual([j[2] for j in rb.call_args[0][0]], [0, 512])  # both blocks read again
        self.assertFalse(os.path.exists(log))
        self.assertEqual(raw.shape, (1024, 8, 2))

    def _noise_profile(self, peak_block):
        nb, ncol = 60, 400
        sr = 900e3 + np.arange(ncol) * 100.
        m = {'utc': np.arange(nb * 512 + 512) / 1600., 'sr': sr}
        lines = np.arange(nb) * 512
        az = np.exp(-(np.arange(nb) - peak_block) ** 2 / (2 * 8. ** 2))
        prof = 1. + 50 * az[:, None] * np.exp(-(sr - 920e3) ** 2 / (2 * 3e3 ** 2))[None, :]
        return prof, lines, m

    def test_noise_cues_flag_an_envelope_cut_by_the_read(self):
        prof, lines, m = self._noise_profile(30)
        c = nt.raw_noise_cues(prof, lines, m, (0, 100), (900e3, 940e3))
        self.assertFalse(c['edge'])
        self.assertLess(abs(c['R'] - 920e3), 1e3)
        prof, lines, m = self._noise_profile(-5)  # peak before the first block read
        self.assertTrue(nt.raw_noise_cues(prof, lines, m, (0, 100), (900e3, 940e3))['edge'])

    def test_noise_cues_without_excess_power_return_none(self):
        prof, lines, m = self._noise_profile(30)
        self.assertIsNone(nt.raw_noise_cues(np.ones_like(prof), lines, m, (0, 100), (900e3, 940e3)))

    def test_find_rrsd_keeps_newest_version_of_each_raw_acquisition(self):
        mk = lambda t0, t1, v: {'title': (f'NISAR_L0_PR_RRSD_010_005_A_018S_{t0}_{t1}_P05023_F_J_{v}'),
                                'links': [{'href': f'https://x/NISAR_L0_PR_RRSD_010_005_A_018S_{t0}_{t1}_P05023_F_J_{v}.h5'}]}
        a, b = ('20260109T125000', '20260109T125130'), ('20260109T125130', '20260109T125300')
        entries = [mk(*a, '001'), mk(*a, '002'), mk(*b, '001'),
                   {'title': 'NISAR_L0_PR_RRSD_010_006_A_018S_20260109T125000_20260109T125130_P05023_F_J_001'}]
        with mock.patch.object(nt, 'cmr', return_value=entries):
            got = sorted(n for n, _ in nt.find_rrsd(GRANULE))
        self.assertEqual(got, sorted([mk(*a, '002')['title'], mk(*b, '001')['title']]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
