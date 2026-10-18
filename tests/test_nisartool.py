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
from pyproj import CRS, Transformer


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
