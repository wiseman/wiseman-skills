#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pyproj", "matplotlib", "requests", "h5py", "earthaccess", "rasterio"]
# ///
"""NISAR (NASA-ISRO L-band radar) tools: find passes over a place, look at HH/HV imagery north-up
(parked aircraft, ships, anything else), find radio-interference blobs and their centres, and read
the interference record NISAR's RFI filter writes into each RSLC product, and locate an in-band
emitter from the raw echoes (L0B RRSD) to about 2 km from one pass.

  nisartool.py scenes LAT LON START END [--release any|provisional|beta] [--all-modes]
  nisartool.py crop LAT LON SIZE_M GRANULE [GRANULE ...] [--gcov] [--looks M] [--markers CSV] [--out PNG]
  nisartool.py blobs LAT LON GRANULE [--size-km 120] [--max 3] [--min-db 6] [--pol HV|HH] [--out PNG]
  nisartool.py rfimeta GRANULE [--pol HV] [--height M] [--tone-frac 0.05] [--out PNG]
  nisartool.py rawfix LAT LON GRANULE [GRANULE ...] [--kind auto|pulse|noise] [--freq-mhz F] [--out PNG]

GRANULE is a NISAR granule name such as
NISAR_L2_PR_GCOV_010_005_A_018_4005_DHDH_A_20260109T125114_20260109T125149_P05023_N_F_J_001 (from
`scenes`). Any of its products (GCOV, GSLC, RSLC) works: each command switches to the product it
needs for the same acquisition. Search needs no account. Reading data needs a free NASA Earthdata
Login: ~/.netrc with a urs.earthdata.nasa.gov entry, or $EARTHDATA_USERNAME/$EARTHDATA_PASSWORD, or
$EARTHDATA_TOKEN. Files are cloud-optimised HDF5 (1.5-26 GB each); the tool reads only the window it
needs over HTTPS and caches it in $NISARTOOL_CACHE (default ~/.cache/nisartool).

How accurate blob, RFI-record and raw-data positions were in tests: references/validation.md.
"""

import argparse, csv, datetime as dt, hashlib, json, math, os, re, sys, tempfile, warnings

import numpy as np
import requests
from pyproj import CRS, Geod, Transformer
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator

warnings.filterwarnings('ignore', category=RuntimeWarning)  # all-NaN blocks, log of zero
CMR = 'https://cmr.earthdata.nasa.gov/search/granules.json'
DATA = 'https://nisar.asf.earthdatacloud.nasa.gov'
SHORT = {('GCOV', 'provisional'): 'NISAR_L2_GCOV_PROVISIONAL_V1', ('GCOV', 'beta'): 'NISAR_L2_GCOV_BETA_V1',
         ('GSLC', 'provisional'): 'NISAR_L2_GSLC_PROVISIONAL_V1', ('GSLC', 'beta'): 'NISAR_L2_GSLC_BETA_V1',
         ('RSLC', 'provisional'): 'NISAR_L1_RSLC_PROVISIONAL_V1', ('RSLC', 'beta'): 'NISAR_L1_RSLC_BETA_V1'}
# polarisation code (first two letters for frequency A, next two for B) -> like-pol first, then cross-pol
POLS = {'DH': ['HH', 'HV'], 'QP': ['HH', 'HV'], 'DV': ['VV', 'VH'], 'SH': ['HH'], 'SV': ['VV']}
POSTING = {0: 'frequency B only, GCOV 80 m', 5: 'GSLC 40x5 m, GCOV 80 m', 20: 'GSLC 10x5 m, GCOV 20 m',
           40: 'GSLC 5x5 m, GCOV 10 m', 77: 'GSLC 2.5x5 m, GCOV 20 m'}
CREDIT = 'Contains NASA-ISRO NISAR data, NASA/JPL-Caltech/ISRO, distributed by ASF DAAC.'
BLOCK_M, SMOOTH_M = 200.0, 2000.0      # blob search: 200 m blocks, 2 km Gaussian smoothing
CROP_MAX_M = 20000                     # crops are for looking at things, not frames
HTTP = requests.Session()
HTTP.headers['User-Agent'] = 'nisartool/0.1'


def die(msg):
    sys.exit(msg)


# $NISARTOOL_CACHE, else the user cache dir ($XDG_CACHE_HOME or ~/.cache), else the temp dir
def _default_cache():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    try:
        os.makedirs(os.path.join(base, 'nisartool'), exist_ok=True)
        if os.access(os.path.join(base, 'nisartool'), os.W_OK):
            return os.path.join(base, 'nisartool')
    except OSError:
        pass
    return os.path.join(tempfile.gettempdir(), 'nisarcache')


CACHE = os.environ.get('NISARTOOL_CACHE') or _default_cache()


def cache_path(key):
    try:
        os.makedirs(CACHE, exist_ok=True)
    except OSError as ex:
        die(f'Cannot create the cache directory {CACHE} ({ex}); set NISARTOOL_CACHE to a writable directory.')
    return os.path.join(CACHE, hashlib.sha1(key.encode()).hexdigest()[:20] + '.npz')


# --- names and catalogue ---------------------------------------------------------------------

def parse_name(g):
    """Fields of a NISAR granule name (JPL D-102255): product, track, direction, frame, mode
    (bandwidth of frequency A and B in MHz), polarisations, start time, release. ASF: PROVISIONAL
    products have CRID 05023 or higher, BETA lower; the letter is not the release (X = experimental
    build, and some X05026 products are in the PROVISIONAL collection, RFI-filtered)."""
    p = g.split('_')
    if len(p) < 15 or p[0] != 'NISAR':
        die(f'{g}: not a NISAR granule name (they start NISAR_L2_PR_GCOV_...; get them from `scenes`)')
    mode = p[8]
    return {'product': p[3], 'track': p[5], 'dir': 'ascending' if p[6] == 'A' else 'descending',
            'frame': p[7], 'bw_a': int(mode[:2]), 'bw_b': int(mode[2:]), 'pols': p[9],
            'start': dt.datetime.strptime(p[11], '%Y%m%dT%H%M%S').replace(tzinfo=dt.timezone.utc),
            'release': 'beta' if int(p[13][1:]) < 5023 else 'provisional'}


def pols_of(g, freq='A'):
    """Polarisations of one frequency, like-pol first (dual, quad or single linear modes only)."""
    code = parse_name(g)['pols'][:2] if freq == 'A' else parse_name(g)['pols'][2:4]
    if code not in POLS:
        die(f'{g}: polarisation mode {code} of frequency {freq} is not supported by this tool (dual, quad or single linear only)')
    return POLS[code]


def frame_pols(g):
    """Polarisations of frequency A, the band the image commands read."""
    if parse_name(g)['bw_a'] == 0:
        die(f'{g}: this mode records only the 5 MHz side band (80 m pixels), too coarse for this; '
            'pick a pass with a 20/40/77 MHz mode in `scenes`')
    return pols_of(g, 'A')


def sibling(g, product):
    """The same acquisition's granule name for another product (GCOV, GSLC, RSLC)."""
    level = 'L1' if product == 'RSLC' else 'L2'
    return re.sub(r'^NISAR_L[12]_(\w\w)_(GCOV|GSLC|RSLC)_', lambda m: f'NISAR_{level}_{m.group(1)}_{product}_', g)


def cmr(params):
    out, hdr = [], {}
    while True:
        r = HTTP.get(CMR, params={**params, 'page_size': 2000}, headers=hdr, timeout=60)
        r.raise_for_status()
        e = r.json()['feed']['entry']
        out += e
        if 'CMR-Search-After' not in r.headers or not e:
            return out
        hdr = {'CMR-Search-After': r.headers['CMR-Search-After']}


def locate(g):
    """HTTPS data URL of a granule, from CMR."""
    for x in cmr({'readable_granule_name': g, 'provider': 'ASF'}):
        for l in x.get('links', []):
            h = l.get('href', '')
            if h.startswith('https') and h.endswith('.h5') and f'/{g}.h5' in h:
                return h
    die(f'{g}: not found in the NASA CMR catalogue (product may not exist for this acquisition)')


def local_crs(lat, lon):
    """North-up azimuthal equidistant frame in metres around a point."""
    return CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84')


# --- data access -----------------------------------------------------------------------------

_fs = None


def h5(url):
    """Open a remote NISAR HDF5 file through an authenticated, block-cached HTTPS session."""
    global _fs
    import h5py, earthaccess
    if _fs is None:
        for strategy in ('environment', 'netrc'):
            try:
                auth = earthaccess.login(strategy=strategy)
            except Exception:  # noqa: BLE001 - earthaccess raises several types
                auth = None
            if auth and getattr(auth, 'authenticated', False):
                break
        else:
            die('NASA Earthdata Login needed to read NISAR data (search works without it). Make a free '
                'account at https://urs.earthdata.nasa.gov, then add "machine urs.earthdata.nasa.gov login '
                'USER password PASS" to ~/.netrc, or set EARTHDATA_USERNAME and EARTHDATA_PASSWORD.')
        _fs = earthaccess.get_fsspec_https_session()
    name = url.rsplit('/', 1)[-1]
    try:
        return h5py.File(_fs.open(url, 'rb', cache_type='blockcache', block_size=4 << 20), 'r')
    except Exception as ex:  # noqa: BLE001 - fsspec/aiohttp/h5py raise many types
        msg, status = str(ex), getattr(ex, 'status', None) or getattr(ex, 'code', None)
        if status in (401, 403) or re.search(r'\b(401|403)\b|Unauthorized|Forbidden|urs\.earthdata', msg):
            die(f'NASA Earthdata Login was refused while opening {name} ({msg[:120]}). '
                'If you use EARTHDATA_TOKEN it may have expired: make a new one at https://urs.earthdata.nasa.gov '
                '(Generate Token), or use ~/.netrc or EARTHDATA_USERNAME/EARTHDATA_PASSWORD instead.')
        if status == 404 or isinstance(ex, FileNotFoundError) or re.search(r'\b404\b', msg):
            die(f'{name}: listed in the catalogue but the file is not there (404); try another pass.')
        die(f'Could not read {name}: {type(ex).__name__}: {msg[:200]} (network or file problem; try again)')


def block_mean(a, b):
    """Mean of each b x b block of a power array, ignoring zero and negative (no-data) pixels."""
    n0, n1 = a.shape[0] // b * b, a.shape[1] // b * b
    a = a[:n0, :n1]
    a[a <= 0] = np.nan
    return np.nanmean(a.reshape(n0 // b, b, n1 // b, b), axis=(1, 3))


def read_window(g, product, pols, lat, lon, half_m, block_m=0):
    """Power (float32) of `pols` in frequency A of a geocoded product (GCOV or GSLC) covering a square
    of +-half_m around a point, optionally averaged into blocks of about block_m. The returned
    product-grid rectangle includes padding outside that local square; callers must mask or
    resample it to enforce the requested boundary. Returns
    (dict pol -> array, x, y, epsg, posting_m). Works in any product CRS (UTM or polar)."""
    gname = sibling(g, product)
    key = cache_path(f'v2|{gname}|{"/".join(pols)}|{lat:.5f}|{lon:.5f}|{half_m:.0f}|{block_m:.0f}')
    if os.path.exists(key):
        z = np.load(key)
        return {k: z[k] for k in pols}, z['x'], z['y'], int(z['epsg']), float(z['post'])
    with h5(locate(gname)) as f:
        grid = f[f'/science/LSAR/{product}/grids/frequencyA']
        x, y, epsg = grid['xCoordinates'][:], grid['yCoordinates'][:], int(grid['projection'][()])
        tr = Transformer.from_crs(local_crs(lat, lon), epsg, always_xy=True)
        s = np.linspace(-half_m, half_m, 9)
        px, py = tr.transform(*(v.ravel() for v in np.meshgrid(s, s)))
        c0, c1 = np.searchsorted(x, px.min()), np.searchsorted(x, px.max())
        r0, r1 = np.searchsorted(-y, -py.max()), np.searchsorted(-y, -py.min())
        c0, r0 = max(0, c0 - 2), max(0, r0 - 2)
        c1, r1 = min(len(x), c1 + 2), min(len(y), r1 + 2)
        if c1 - c0 < 3 or r1 - r0 < 3:
            die(f'{gname}: the point is outside this frame')
        names = {k: k + k if product == 'GCOV' else k for k in pols}  # GCOV stores HHHH, GSLC HH
        missing = [v for v in names.values() if v not in grid]
        if missing:
            die(f'{gname}: no {"/".join(missing)} in frequency A (this mode records {parse_name(g)["pols"]})')
        post = min(abs(x[1] - x[0]), abs(y[1] - y[0]))
        x, y = x[c0:c1], y[r0:r1]
        b = max(1, int(round(block_m / post))) if block_m else 1
        out = {}
        for k, v in names.items():  # one full-resolution array in memory at a time
            a = grid[v][r0:r1, c0:c1]
            a = (a.real ** 2 + a.imag ** 2) if np.iscomplexobj(a) else a.astype(np.float32)
            out[k] = block_mean(a, b) if b > 1 else a
    if b > 1:
        x, y, post = x[:len(x) // b * b].reshape(-1, b).mean(1), y[:len(y) // b * b].reshape(-1, b).mean(1), post * b
    np.savez(key, x=x, y=y, epsg=epsg, post=post, **out)
    return out, x, y, epsg, float(post)


def to_local(planes, x, y, epsg, lat, lon, half_m, px_m):
    """Resample power arrays from the product grid to a north-up grid in metres around a point
    (the product grid is UTM at most latitudes but polar stereographic near the poles, rotated by
    up to ~100 degrees)."""
    e = np.arange(-half_m, half_m, px_m) + px_m / 2
    E, N = np.meshgrid(e, e[::-1])
    X, Y = Transformer.from_crs(local_crs(lat, lon), epsg, always_xy=True).transform(E, N)
    rc = [(Y - y[0]) / (y[1] - y[0]), (X - x[0]) / (x[1] - x[0])]
    return [ndimage.map_coordinates(p, rc, order=1, cval=np.nan) for p in planes]


def window_mask(x, y, epsg, lat, lon, half_m):
    """Product-grid pixel centres inside the requested square in local east/north metres."""
    E, N = Transformer.from_crs(epsg, local_crs(lat, lon), always_xy=True).transform(*np.meshgrid(x, y))
    return (np.abs(E) <= half_m) & (np.abs(N) <= half_m)


def db(a):
    return 10 * np.log10(np.where(a > 0, a, np.nan))


def read_markers(path, lat, lon, half_m):
    """Markers from a CSV with lat and lon columns (and optional label), such as an ADS-B track
    CSV, as (m east, m north, label) inside the crop."""
    rows = [r for r in csv.DictReader(open(path)) if r.get('lat') and r.get('lon')]
    if not rows:
        die(f'{path}: no rows with lat and lon columns (the CSV needs a header line naming them)')
    me, mn = Transformer.from_crs(4326, local_crs(lat, lon), always_xy=True).transform(
        [float(r['lon']) for r in rows], [float(r['lat']) for r in rows])
    marks = [(e, n, r.get('label') or '') for e, n, r in zip(me, mn, rows) if abs(e) < half_m and abs(n) < half_m]
    print(f'{len(marks)} of {len(rows)} markers fall inside the crop')
    return marks


# --- commands --------------------------------------------------------------------------------

def cmd_scenes(a):
    start = a.start if 'T' in a.start else a.start + 'T00:00:00Z'
    end = a.end if 'T' in a.end else a.end + 'T23:59:59Z'
    rels = ['provisional', 'beta'] if a.release == 'any' else [a.release]
    latest = {}  # a reprocessed acquisition appears again with a higher product counter (_002); keep the latest
    for e in cmr({'short_name[]': [SHORT[('GCOV', r)] for r in rels], 'point': f'{a.lon},{a.lat}', 'temporal': f'{start},{end}'}):
        g = e['title']
        if g.startswith('NISAR_') and g > latest.get(g[:-4], ''):
            latest[g[:-4]] = g
    rows = sorted(((g, parse_name(g)) for g in latest.values()), key=lambda r: r[1]['start'])
    shown, last = 0, None
    for g, n in rows:
        if not a.all_modes and n['bw_a'] == 0:
            continue
        # Adjacent frames of one acquisition overlap, so a point near a frame edge is in two granules
        # about 35 s apart: one pass, not two.
        same = (g.split('_')[4], n['track'], n['dir'], n['release'])
        dup = last and last[0] == same and (n['start'] - last[1]).total_seconds() < 120
        last = (same, n['start'])
        if dup:
            print(f"    also in frame {n['frame']} of the same pass (frames overlap; use either): {g}")
            continue
        t, rel = n['start'], n['release']
        mode = f"{n['bw_a']}+{n['bw_b']} MHz" if n['bw_a'] else f"B only {n['bw_b']} MHz"
        cross = len(POLS.get(n['pols'][:2], [])) == 2
        solar = (t + dt.timedelta(hours=a.lon / 15)).strftime('%H:%M')
        print(f"{t:%Y-%m-%d %H:%M}Z (~{solar} local solar)  {n['dir']:10s} track {n['track']} frame {n['frame']}  "
              f"{mode:11s} {n['pols']}{'' if cross else ' (no cross-pol)'}  {rel.upper()} "
              f"({'RFI filter off' if rel == 'beta' else 'RFI filter on'})  {POSTING.get(n['bw_a'], '')}")
        print(f"    {g}")
        shown += 1
    if not shown:
        print('No NISAR GCOV granules over this point in that period (try --release any, a longer period, or --all-modes).')
    print(f'\n{shown} pass(es). BETA: a subset of passes from Oct 2025 to Jan 2026, uncalibrated, no RFI removed. '
          'PROVISIONAL: every pass from 17 Jun 2026 plus selected frames back to Oct 2025, calibrated and RFI-filtered; '
          'no data 27 Jul-10 Aug 2026. Public browse image of a granule: '
          f'{DATA}/BROWSE/<SHORT_NAME>/<GRANULE>/<GRANULE>_LATLON.png (PROVISIONAL) or <GRANULE>.png (BETA).')


def cmd_crop(a):
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    if a.size > CROP_MAX_M:
        print(f'note: crop capped at {CROP_MAX_M} m across (crops are for looking at things; use `blobs` for wide views)')
    half = min(a.size, CROP_MAX_M) / 2
    product = 'GCOV' if a.gcov else 'GSLC'
    marks = read_markers(a.markers, a.lat, a.lon, half) if a.markers else []
    rows = []
    for g in a.granule:
        n, pols = parse_name(g), frame_pols(g)
        d, x, y, epsg, post = read_window(g, product, pols, a.lat, a.lon, half)
        px_m = max(post, 5.0) if product == 'GSLC' else post
        if product == 'GSLC':  # average to about 10 x 10 m to tame speckle
            win = max(1, round(a.looks / abs(y[1] - y[0]))), max(1, round(a.looks / abs(x[1] - x[0])))
            d = {k: ndimage.uniform_filter(np.nan_to_num(v), win) for k, v in d.items()}
        dbs = [db(p) for p in to_local([d[k] for k in pols], x, y, epsg, a.lat, a.lon, half, px_m)]
        rows.append((n, pols, dbs))
        for k, im in zip(pols, dbs):
            v = im[np.isfinite(im)]
            if v.size:
                print(f"{g[17:60]} {k}: median {np.median(v):.1f} dB, 99th pct {np.percentile(v, 99):.1f} dB")
    ncol = 3 if any(len(r[1]) == 2 for r in rows) else 1
    fig, ax = plt.subplots(len(rows), ncol, figsize=(5.6 * ncol, 5.6 * len(rows) + 0.8), squeeze=False)
    ext = [-half, half, -half, half]
    for i, (n, pols, dbs) in enumerate(rows):
        when = f"{n['start']:%Y-%m-%d %H:%MZ} {n['dir'][:4]} t{n['track']} {n['bw_a']} MHz"
        panels = [(dbs[0], pols[0], 'gray')]
        if len(dbs) == 2:
            panels += [(dbs[1], pols[1], 'gray'), (dbs[1] - dbs[0], f'{pols[1]}-{pols[0]}', 'magma')]
        for j, (im, lab, cm) in enumerate(panels):
            axx = ax[i, j]
            v = im[np.isfinite(im)]
            if not v.size:  # crop falls in the frame's masked (no-data) corner
                axx.text(0.5, 0.5, 'no valid data here', ha='center', transform=axx.transAxes); axx.set_title(f'{when}\n{lab}', fontsize=9, loc='left')
                continue
            lo, hi = (np.percentile(v, 2), np.percentile(v, 99.5)) if '-' not in lab else (-22, 0)
            axx.imshow(im, extent=ext, cmap=cm, vmin=lo, vmax=hi, interpolation='nearest')
            axx.set_title(f'{when}\n{lab}, {lo:.0f} to {hi:.0f} dB', fontsize=9, loc='left')
            axx.text(0.97, 0.96, 'N↑', transform=axx.transAxes, color='yellow', ha='right', va='top', weight='bold')
            for me, mn, mlab in marks:
                axx.plot(me, mn, 'o', mfc='none', mec='cyan', ms=9, mew=1.3)
                if mlab:
                    axx.text(me, mn, f'  {mlab}', color='cyan', fontsize=7, va='center')
            axx.set_xlabel('m east', fontsize=8); axx.set_ylabel('m north', fontsize=8); axx.tick_params(labelsize=7)
        for j in range(len(panels), ncol):
            ax[i, j].axis('off')
    fig.suptitle(f'NISAR {product} around {a.lat:.5f}, {a.lon:.5f} ({2 * half / 1000:.1f} km), north-up', x=0.01, ha='left')
    fig.text(0.01, 0.003, CREDIT, fontsize=8, color='#444')
    fig.tight_layout(rect=(0, 0.015, 1, 0.97))
    fig.savefig(a.out, dpi=100, facecolor='white')
    print(f'wrote {a.out}')


def find_blobs(V, valid, block_m, min_db, nmax):
    sm = ndimage.gaussian_filter(np.where(valid, db(V), np.nanmedian(db(V[valid]))), SMOOTH_M / block_m)
    bg = float(np.nanpercentile(sm[valid], 20))
    work = np.where(valid, sm, -np.inf)
    edge = ~ndimage.binary_erosion(valid, iterations=2)
    edge[[0, -1], :] = True
    edge[:, [0, -1]] = True
    found = []
    for _ in range(nmax):
        pk = np.unravel_index(np.argmax(work), work.shape)
        if not np.isfinite(work[pk]) or work[pk] - bg < min_db:
            break
        lab, _ = ndimage.label(np.isfinite(work) & (sm > bg + min_db))
        reg = lab == lab[pk]
        w = np.where(reg, 10 ** ((sm - bg) / 10), 0)
        cy, cx = (np.indices(sm.shape) * w).sum((1, 2)) / w.sum()
        found.append({'peak_rc': pk, 'centre_rc': (cy, cx), 'peak_db': float(sm[pk] - bg), 'area_px': int(reg.sum()),
                      'truncated': bool((reg & edge).any()), 'region': reg})
        work[ndimage.binary_dilation(reg, iterations=3)] = -np.inf
    return np.where(valid, sm - bg, np.nan), found


def cmd_blobs(a):
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    g = sibling(a.granule, 'GCOV')
    n, pols = parse_name(g), frame_pols(g)
    layer = pols[-1] if a.pol == 'HV' else pols[0]   # --pol HV means cross-pol (VH in VV/VH modes)
    if a.pol == 'HV' and len(pols) == 1:
        print(f'note: no cross-pol channel in this mode; searching {layer} instead')
    d, xs, ys, epsg, blk = read_window(g, 'GCOV', [layer], a.lat, a.lon, a.size_km * 500, block_m=BLOCK_M)
    V = d[layer]
    valid = np.isfinite(V) & (V > 0) & window_mask(xs, ys, epsg, a.lat, a.lon, a.size_km * 500)
    V = np.where(valid, V, np.nan)
    if valid.sum() < 100:
        die('Almost no valid data in this window (the point may be at the frame edge).')
    sm, found = find_blobs(V, valid, blk, a.min_db, a.max)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    X0, Y0 = Transformer.from_crs(4326, epsg, always_xy=True).transform(a.lon, a.lat)
    km = lambda xy: ((xy[0] - X0) / 1e3, (xy[1] - Y0) / 1e3)
    res = []
    print(f"{g}\n{n['start']:%Y-%m-%d %H:%MZ} {n['dir']}, {'BETA (no RFI filter)' if n['release'] == 'beta' else 'PROVISIONAL (RFI filtered)'}; "
          f"{layer} smoothed {SMOOTH_M / 1000:.0f} km, background = 20th percentile")
    if not found:
        print(f'No blob more than {a.min_db} dB above background in this {a.size_km:.0f} km window.')
    for i, f in enumerate(found, 1):
        (pr, pc), (cy, cx) = f['peak_rc'], f['centre_rc']
        f['peak_xy'] = xs[pc], ys[pr]
        f['centre_xy'] = np.interp(cx, np.arange(len(xs)), xs), np.interp(cy, np.arange(len(ys)), ys)
        (plon, plat), (clon, clat) = back.transform(*f['peak_xy']), back.transform(*f['centre_xy'])
        area = f['area_px'] * (blk / 1000) ** 2
        res.append({'blob': i, 'peak_lat': round(plat, 5), 'peak_lon': round(plon, 5), 'centre_lat': round(clat, 5),
                    'centre_lon': round(clon, 5), 'peak_db_above_bg': round(f['peak_db'], 1), 'area_km2': round(area),
                    'runs_off_data': f['truncated']})
        print(f"blob {i}: brightest point {plat:.4f}, {plon:.4f}  centre {clat:.4f}, {clon:.4f}  "
              f"+{f['peak_db']:.1f} dB  {area:.0f} km2{'  (runs off the data or window: use the brightest point, not the centre)' if f['truncated'] else ''}")
    js = os.path.splitext(a.out)[0] + '.json'
    with open(js, 'w', encoding='utf-8') as out:
        json.dump({'granule': g, 'lat': a.lat, 'lon': a.lon, 'size_km': a.size_km, 'channel': layer, 'blobs': res}, out, indent=1)
    fig, ax = plt.subplots(1, 2, figsize=(14, 6.6))
    ext = [(xs[0] - X0) / 1e3, (xs[-1] - X0) / 1e3, (ys[-1] - Y0) / 1e3, (ys[0] - Y0) / 1e3]
    raw = db(V) - np.nanpercentile(db(V[valid]), 20)
    ax[0].imshow(raw, extent=ext, cmap='gray', vmin=-6, vmax=max(12, np.nanpercentile(raw, 99.5)))
    ax[1].imshow(sm, extent=ext, cmap='magma', vmin=0, vmax=max(12, np.nanmax(sm)))
    for i, f in enumerate(found, 1):
        for axx in ax:
            axx.plot(*km(f['peak_xy']), 'x', color='lime', ms=11, mew=2)
            axx.plot(*km(f['centre_xy']), 'o', mfc='none', mec='yellow', ms=11, mew=2)
            axx.text(*km(f['peak_xy']), f'  {i}', color='white', fontsize=10)
        ax[1].contour(f['region'].astype(float), levels=[0.5], colors='white', linewidths=0.7, extent=[ext[0], ext[1], ext[3], ext[2]])
    for axx in ax:
        axx.plot(0, 0, '+', color='cyan', ms=14, mew=2)
        axx.set_xlabel(f'km from {a.lat:.3f}, {a.lon:.3f} (map grid EPSG:{epsg}, may be rotated near the poles)', fontsize=8)
    ax[0].set_title(f'{layer} (dB above background), {blk:.0f} m blocks', loc='left', fontsize=10)
    ax[1].set_title(f'smoothed {SMOOTH_M / 1000:.0f} km; x brightest point, o centre, white: >{a.min_db} dB', loc='left', fontsize=10)
    fig.suptitle(f"NISAR interference blobs: {n['start']:%Y-%m-%d %H:%MZ} {n['dir']}, track {n['track']} frame {n['frame']}; cyan + = search point", x=0.01, ha='left')
    fig.text(0.01, 0.003, CREDIT, fontsize=8, color='#444')
    fig.tight_layout(rect=(0, 0.02, 1, 0.95))
    fig.savefig(a.out, dpi=95, facecolor='white')
    print(f'wrote {a.out} and {js}')


def squint_locator(f, height, freq):
    """Map a raw-data time and slant range to the ground. NISAR's beam points slightly forward (Doppler
    centroid ~960 Hz), so an emitter is heard most when it is ahead of the zero-Doppler line; the
    RFI record's times are raw times, so the look vector is squinted forward by the Doppler centroid
    and intersected with the ellipsoid raised by `height`."""
    from scipy.interpolate import CubicSpline
    o = '/science/LSAR/RSLC/metadata/orbit/'
    ot = f[o + 'time'][()]
    pos, vel = CubicSpline(ot, f[o + 'position'][()]), CubicSpline(ot, f[o + 'velocity'][()])
    fa = lambda path: path if path in f else path.replace(f'frequency{freq}', 'frequencyA')  # B falls back to A (same beam)
    dc = fa(f'/science/LSAR/RSLC/metadata/processingInformation/parameters/frequency{freq}/dopplerCentroid')
    pp = dc.rsplit('/', 1)[0] + '/'
    fdc = RegularGridInterpolator((f[pp + 'zeroDopplerTime'][()], f[pp + 'slantRange'][()]), f[dc][()],
                                  bounds_error=False, fill_value=None)
    lam = LIGHT / float(f[fa(f'/science/LSAR/RSLC/swaths/frequency{freq}/processedCenterFrequency')][()])
    look = f['/science/LSAR/identification/lookDirection'][()]
    side = -1.0 if (look.decode() if hasattr(look, 'decode') else str(look)).lower().startswith('l') else 1.0
    to_lla = Transformer.from_crs(4978, 4979, always_xy=True)

    def ground(t, R):
        S, V = pos(t), vel(t)
        a_ = V / np.linalg.norm(V)
        n = -S / np.linalg.norm(S); n -= a_ * (n @ a_); n /= np.linalg.norm(n)
        cross = np.cross(a_, n) * -side   # left-looking: the side to the left of the velocity vector
        sq = float(fdc((t, R))) * lam / (2 * np.linalg.norm(V))
        c = math.sqrt(max(0.0, 1 - sq * sq))
        lo, hi = math.radians(1), math.radians(80)
        for _ in range(50):
            th = (lo + hi) / 2
            P = S + R * (sq * a_ + c * (math.cos(th) * n + math.sin(th) * cross))
            alt = to_lla.transform(*P)[2]
            lo, hi = (lo, th) if alt > height else (th, hi)
        lon, lat, _ = to_lla.transform(*P)
        return round(float(lat), 4), round(float(lon), 4)
    return ground


def hotspots(m, thr):
    """Local maxima of a smoothed time x range map above a threshold, as (time, range) indices."""
    if m.shape[0] < 5:  # short tail chunks give spurious peaks
        return []
    mm = ndimage.gaussian_filter(np.nan_to_num(m), 2.0)
    peaks = (mm == ndimage.maximum_filter(mm, size=11)) & (mm > thr)
    return [tuple(q) for q in np.argwhere(peaks)]


def cmd_rfimeta(a):
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    g = sibling(a.granule, 'RSLC')
    n = parse_name(g)
    rel = n['release']
    pol = a.pol or pols_of(g, 'A' if n['bw_a'] else 'B')[-1]   # default: cross-pol
    rfi = lambda fr, p: f'/science/LSAR/RSLC/metadata/RFI/frequency{fr}/{p}'
    out = {'granule': g, 'release': rel, 'pol': pol, 'freqs': {}}
    with h5(locate(g)) as f:
        alg = '/science/LSAR/RSLC/metadata/processingInformation/algorithms/rfiMitigation'
        mitig = f[alg][()] if alg in f else None
        mitig = mitig.decode() if hasattr(mitig, 'decode') else mitig
        freqs = [fr for fr in 'AB' if rfi(fr, pol) in f]
        if not freqs:
            have = [f'{fr}:{p}' for fr in 'AB' for p in ('HH', 'HV', 'VV', 'VH') if rfi(fr, p) in f]
            die(f'{g}: no RFI record for {pol}. ' + (f'Records present: {", ".join(have)}; pass one of those with --pol.' if have
                else 'This product has no RFI record (BETA products have none: use `blobs` on BETA imagery instead).'))
        fig, ax = plt.subplots(len(freqs), 3, figsize=(18, 4.8 * len(freqs)), squeeze=False)
        for fi, fr in enumerate(freqs):
            ground = squint_locator(f, a.height, fr)
            grp = f[rfi(fr, pol)]
            chunks = [grp[k] for k in sorted(grp)]  # raw-data chunks; range blocks can differ slightly between them
            fq = chunks[0]['frequency'][()] / 1e6
            has_sm = all('signalMean' in q for q in chunks)
            parts = [{'t': q['nativeDopplerTime'][()], 'r': q['slantRange'][()], 'hc': q['hitCount'][()],
                      'sm': db(q['signalMean'][()]) if has_sm else None} for q in chunks]
            t_first = min(p['t'][0] for p in parts)
            framewide = sum(p['hc'].sum((0, 1)) for p in parts) / sum(p['hc'].shape[0] * p['hc'].shape[1] for p in parts)
            tones = ndimage.binary_dilation(framewide > a.tone_frac, iterations=4)  # plus neighbouring bins the tone leaks into
            bins_used = int((~tones).sum())
            if bins_used:
                w = (~tones) / bins_used
                for p in parts:
                    p['share'] = p['hc'] @ w   # share of flagged cells outside the ever-present tones
                all_share = np.concatenate([p['share'][:, 3:-3].ravel() for p in parts])
                med = float(np.median(all_share))
            sm_med = float(np.nanmedian(np.concatenate([p['sm'].ravel() for p in parts]))) if has_sm else 0.0
            flag, level = [], []
            for p in parts:
                if bins_used:
                    p['share'][:, :3] = 0; p['share'][:, -3:] = 0  # the swath's near and far edge blocks are always hot
                    flag += [(p['share'][ti, ri], p, ti, ri) for ti, ri in hotspots(p['share'], max(5 * med, 0.01))]
                if has_sm:
                    p['lev'] = p['sm'] - sm_med
                    level += [(p['lev'][ti, ri], p, ti, ri) for ti, ri in hotspots(p['lev'], 10.0)]

            def flag_fields(v, p, ti, ri):  # what the filter flagged around the hotspot, tones removed
                cell = p['hc'][max(0, ti - 2):ti + 3, max(0, ri - 2):ri + 3].reshape(-1, p['hc'].shape[-1]).mean(0)
                cell[tones] = 0
                return {'flagged_share': round(float(v), 4), 'main_freqs_MHz': [round(float(fq[j]), 2) for j in np.argsort(cell)[::-1][:3]],
                        '_spec': cell}

            def top5(cands, extra):  # geolocate only the strongest, skipping any off the geolocation grid
                hs = []
                for v, p, ti, ri in sorted(cands, key=lambda c: -c[0]):
                    lat, lon = ground(p['t'][ti], p['r'][ri])
                    if math.isfinite(lat) and math.isfinite(lon):
                        hs.append({'time_s': round(float(p['t'][ti] - t_first), 1), 'slant_km': round(float(p['r'][ri]) / 1e3, 1),
                                   'lat': lat, 'lon': lon, **extra(v, p, ti, ri)})
                    if len(hs) == 5:
                        break
                return hs
            info = {'tones_everywhere_MHz': sorted({round(float(v), 1) for v in fq[framewide > a.tone_frac]}),
                    'flag_status': 'ok' if bins_used else 'no_frequency_bins',
                    'frequency_bins_used': bins_used, 'frequency_bins_total': int(fq.size), 'tone_frac': a.tone_frac,
                    'flag_hotspots': top5(flag, flag_fields),
                    'level_hotspots': top5(level, lambda v, p, ti, ri: {'level_db_above_median': round(float(v), 1)})}
            top_spec = info['flag_hotspots'][0]['_spec'] if info['flag_hotspots'] else None
            for h in info['flag_hotspots']:
                del h['_spec']
            vmax_s = max(1, np.percentile(100 * all_share, 99.5)) if bins_used else None
            lev_max = max(10, np.nanpercentile(np.concatenate([p['lev'].ravel() for p in parts]), 99.5)) if has_sm else None
            for p in parts:
                T, R = p['t'] - t_first, p['r'] / 1e3
                if bins_used:
                    m0 = ax[fi, 0].pcolormesh(T, R, 100 * p['share'].T, cmap='magma', vmin=0, vmax=vmax_s, shading='nearest')
                if has_sm:
                    m1 =ax[fi, 1].pcolormesh(T, R, p['lev'].T, cmap='viridis', vmin=-3, vmax=lev_max, shading='nearest')
            if bins_used:
                plt.colorbar(m0, ax=ax[fi, 0], label='% of cells flagged')
            else:
                ax[fi, 0].text(0.5, 0.5, 'Flag analysis unavailable:\nno frequency bins remain after tone exclusion.\nThis is not evidence of no interference.',
                               ha='center', va='center', transform=ax[fi, 0].transAxes, fontsize=9)
                ax[fi, 0].set_axis_off()
            ax[fi, 0].set_title(f'{fr}: share flagged (tones flagged across the whole pass excluded)', loc='left', fontsize=9)
            if has_sm:
                plt.colorbar(m1, ax=ax[fi, 1], label='dB above pass median')
                ax[fi, 1].set_title(f"{fr}: detector's typical level (signalMean)", loc='left', fontsize=9)
            for hs, c in ((info['flag_hotspots'], 'cyan'), (info['level_hotspots'], 'white')):
                for h in hs:
                    for axx in (ax[fi, :2] if bins_used else ax[fi, 1:2]):
                        axx.plot(h['time_s'], h['slant_km'], 'o', mfc='none', mec=c, ms=9)
            out['freqs'][fr] = info
            ax[fi, 2].plot(fq, 100 * framewide, color='#888', lw=1, label='whole pass')
            if top_spec is not None:
                ax[fi, 2].plot(fq, 100 * top_spec, color='#d62728', lw=1, label='top flag hotspot (tones removed)')
            if fq.min() < 1227.6 < fq.max():
                ax[fi, 2].axvline(1227.6, color='green', ls='--', lw=1)
            ax[fi, 2].set_xlabel('MHz'); ax[fi, 2].set_ylabel('% of pulses flagged'); ax[fi, 2].legend(fontsize=8)
            ax[fi, 2].set_title(f'{fr}: flagged share by frequency (green: GPS L2)', loc='left', fontsize=9)
            for axx in ax[fi, :2]:
                axx.set_xlabel('seconds from start of the RFI record'); axx.set_ylabel('slant range (km)')
    fig.suptitle(f"NISAR RFI record {g[17:75]} ({rel}; {pol}). Circles: cyan = flag hotspots, white = typical-level hotspots", x=0.01, ha='left')
    fig.text(0.01, 0.003, CREDIT, fontsize=8, color='#444')
    fig.tight_layout(rect=(0, 0.015, 1, 0.96))
    fig.savefig(a.out, dpi=90, facecolor='white')
    js = os.path.splitext(a.out)[0] + '.json'
    with open(js, 'w', encoding='utf-8') as stream:
        json.dump(out, stream, indent=1)
    print(f"{g} ({rel}{'; RFI mitigation: ' + str(mitig) if mitig else ''})")
    for fr, info in out['freqs'].items():
        print(f'frequency {fr} {pol}: tones flagged across the whole pass (MHz): {info["tones_everywhere_MHz"] or "none"}')
        if info['flag_status'] == 'no_frequency_bins':
            print('  Flag analysis unavailable: no frequency bins remain after tone exclusion. '
                  'This is not evidence of no interference; inspect the full spectrum and signalMean if available.')
        for h in info['flag_hotspots']:
            print(f"  flag hotspot  t={h['time_s']:5.1f}s R={h['slant_km']:6.1f}km -> {h['lat']:.4f}, {h['lon']:.4f}  "
                  f"{100 * h['flagged_share']:.1f}% flagged, mostly {h['main_freqs_MHz']} MHz")
        for h in info['level_hotspots']:
            print(f"  level hotspot t={h['time_s']:5.1f}s R={h['slant_km']:6.1f}km -> {h['lat']:.4f}, {h['lon']:.4f}  "
                  f"+{h['level_db_above_median']:.1f} dB (persistent broadband interference raises this)")
    print('Ground positions are rough: the strongest hotspot was 11-24 km from 3 of 4 known radars and 97 km from the '
          'fourth, and weaker hotspots are often spurious. Use this to learn that an emitter was present and its frequency, '
          'not where it is; for position run `rawfix` with a hotspot as the starting point (or `blobs` on BETA products).')
    print(f'wrote {a.out} and {js}')


# --- raw data (L0B RRSD): locating an in-band emitter ----------------------------------------
#
# NISAR's receive beam (SweepSAR) is steered across the swath in step with fast time, so within any
# received pulse the emitter's power peaks at the sample whose slant range points the beam at it:
# that hump gives the emitter's slant range from one pass. The azimuth envelope of the emitter's power
# gives the time the beam crossed it, which with the Doppler centroid fixes the along-track position.
# The RFI filter acts only on processed products, so this works on PROVISIONAL passes too.

RRSD_SHORT = 'NISAR_L0B_RRSD_PROVISIONAL_V1'
LIGHT = 299_792_458.0
RAW_BIAS_M = 910.0                 # the range cue read 0.91 km short on average at 8 known radars (sd 0.70 km)
RAW_SD_R, RAW_SD_T = 700.0, 0.21   # spread of the range and beam-crossing-time cues over those radars
RAW_LINES = 14000                  # pulse mode: lines either side of the beam crossing (~7 s)
RAW_COLS = 12288                   # pulse mode: range samples read (24 chunks of 512)
NOISE_LINES = 48000                # noise mode: lines either side (~25 s), every range sample
RAW_CHUNK = 512                    # HDF5 chunk of the raw echoes, lines and samples: reads go by whole chunks
FDC_NOMINAL = 955.0                # Doppler centroid without an RSLC: 934-972 Hz on every pass seen
_ECEF = Transformer.from_crs(4979, 4978, always_xy=True)
_LLH = Transformer.from_crs(4978, 4979, always_xy=True)
_GEOD = Geod(ellps='WGS84')


def _str(v):
    return v.decode() if hasattr(v, 'decode') else str(v)


def _sample_rate(m):
    return LIGHT / (2 * (m['sr'][1] - m['sr'][0]))


def _line_blocks(l0, l1):
    """(start, end) of reads of lines l0..l1 split at chunk boundaries."""
    s = [l0] + list(range(l0 - l0 % RAW_CHUNK + RAW_CHUNK, l1, RAW_CHUNK))
    return list(zip(s, s[1:] + [l1]))


def ecef(lat, lon, h):
    return np.array(_ECEF.transform(lon, lat, h))


def llh(x):
    lon, lat, h = _LLH.transform(*x)
    return lat, lon, h


class RawOrbit:
    def __init__(self, m):
        from scipy.interpolate import CubicSpline
        self.p, self.v = CubicSpline(m['ot'], m['opos']), CubicSpline(m['ot'], m['ovel'])

    def rdot(self, t, x):
        d = self.p(t) - x
        r = np.linalg.norm(d, axis=-1)
        return r, np.sum(d * self.v(t), axis=-1) / r


def find_rrsd(g):
    """Names and URLs of the raw-data (L0B RRSD) granules that overlap the acquisition of a granule:
    the newest processing version of each raw acquisition (a frame can straddle two)."""
    n, p = parse_name(g), g.split('_')
    iso = lambda s: dt.datetime.strptime(s, '%Y%m%dT%H%M%S').strftime('%Y-%m-%dT%H:%M:%SZ')
    side = 'A' if n['dir'] == 'ascending' else 'D'
    best = {}
    for e in cmr({'short_name': RRSD_SHORT, 'temporal': f'{iso(p[11])},{iso(p[12])}'}):
        q = e['title'].split('_')
        if len(q) > 9 and q[5] == n['track'] and q[6] == side:
            acq = (q[8], q[9])   # raw start and stop times
            if acq not in best or e['title'] > best[acq]['title']:
                best[acq] = e
    if not best:
        die(f'{g}: no raw-data (RRSD) granule for this acquisition in the catalogue (RRSD is public for '
            'PROVISIONAL passes, 2-10 h after the pass; some early passes have none)')
    out = []
    for e in best.values():
        h = next((l['href'] for l in e.get('links', []) if l.get('href', '').startswith('https')
                  and l['href'].endswith(f"/{e['title']}.h5")), None)
        if h:
            out.append((e['title'], h))
    if not out:
        die(f"{', '.join(e['title'] for e in best.values())}: no HTTPS data link in the catalogue")
    return out


def _epoch(units):
    m = re.search(r'(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)', units or '')
    return dt.datetime.fromisoformat(f'{m.group(1)}T{m.group(2)}').replace(tzinfo=dt.timezone.utc) if m else None


def raw_meta(name, url, pol):
    """Orbit, pulse times, slant-range axis, centre frequency and BFPQ table of an RRSD."""
    key = cache_path(f'rawmeta|{name}|{pol}')
    if os.path.exists(key):
        z = np.load(key, allow_pickle=False)
        return {k: (z[k].item() if z[k].ndim == 0 else z[k]) for k in z.files}
    with h5(url) as f:
        R = f['science/LSAR/RRSD']
        o, tx = R['lowRateTelemetry/orbit'], R[f'swaths/frequencyA/tx{pol[0]}']
        path = f'swaths/frequencyA/tx{pol[0]}/rx{pol[1]}/{pol}'
        if path not in R:
            die(f'{name}: no {pol} raw echoes in frequency A')
        ds = R[path]
        units = tx['UTCtime'].attrs.get('units', b'')
        units, ounits = _str(units), _str(o['time'].attrs.get('units', b''))
        ep, eo = _epoch(units), _epoch(ounits)
        if ounits and (ep is None) != (eo is None):   # one clock's epoch readable, the other's not
            die(f'{name}: cannot read the epoch of the pulse times ({units!r}) or the orbit ({ounits!r})')
        shift = (eo - ep).total_seconds() if ep and eo else 0.0   # orbit times onto the pulse clock
        look = f['science/LSAR/identification/lookDirection'][()] if 'science/LSAR/identification/lookDirection' in f else b'left'
        m = dict(ot=o['time'][:] + shift, opos=o['position'][:], ovel=o['velocity'][:], utc=tx['UTCtime'][:],
                 sr=tx['slantRange'][:], fc=float(tx['centerFrequency'][()]), lut=ds.parent['BFPQLUT'][:],
                 path=f'science/LSAR/RRSD/{path}', ncol=int(ds.shape[1]),
                 units=units, left=_str(look).lower().startswith('l'))
    np.savez(key, **m)
    return m


def raw_dc(g, m):
    """Native Doppler centroid table (zero-Doppler time on the RRSD's clock, slant range) from the
    acquisition's RSLC; None if there is no RSLC."""
    rs = sibling(g, 'RSLC')
    key = cache_path(f'rawdc|{rs}')
    if os.path.exists(key):
        z = np.load(key)
        return {k: z[k] for k in z.files}
    try:
        url = locate(rs)
    except SystemExit:
        return None
    with h5(url) as f:
        pp = '/science/LSAR/RSLC/metadata/processingInformation/parameters/'
        zt = f[pp + 'zeroDopplerTime']
        u = zt.attrs.get('units', b'')
        e1, e0 = _epoch(_str(u)), _epoch(m['units'])
        shift = (e1 - e0).total_seconds() if e1 and e0 else 0.0   # the two clocks' epochs can differ
        d = dict(zt=zt[()] + shift, sr=f[pp + 'slantRange'][()], dc=f[pp + 'frequencyA/dopplerCentroid'][()])
    np.savez(key, **d)
    return d


_OPEN = {}   # worker process: remote files kept open between blocks


def _raw_job(job):
    """Worker: read one block of raw lines (codes into a shared .npy, or a mean power profile, optionally
    of one band: (offset Hz, bandwidth Hz, sampling rate))."""
    url, path, a, b, c0, c1, out, l0, band, lut = job
    try:
        if url not in _OPEN:
            _OPEN[url] = h5(url)
        z = _OPEN[url][path][a:b, c0:c1]
        if out:
            mm = np.load(out, mmap_mode='r+')
            mm[a - l0:a - l0 + len(z)] = np.stack([z['r'], z['i']], -1)
            mm.flush()
            return a, None
        if band is None:
            return a, (lut[z['r']] ** 2 + lut[z['i']] ** 2).mean(axis=0).astype(np.float32)
        x = _bandpass(lut[z['r']] + 1j * lut[z['i']], *_band(z.shape[1], band[2], band[0], band[1]))
        return a, (np.abs(x) ** 2).mean(axis=0).astype(np.float32)
    except SystemExit as ex:   # die() in a worker: hand the message back
        raise RuntimeError(str(ex)) from None


def raw_blocks(jobs, workers, log=None, stall_s=300):
    """Run raw-read jobs in worker processes. Remote reads sometimes stall; when no block finishes for
    stall_s seconds the workers are killed and the unfinished blocks retried (3 rounds)."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
    done, todo = {}, list(jobs)
    for attempt in range(3):
        if not todo:
            break
        ex = ProcessPoolExecutor(workers, mp_context=mp.get_context('spawn'))
        futs = {ex.submit(_raw_job, j): j for j in todo}
        pending, t0 = set(futs), dt.datetime.now()
        try:
            while pending:
                fin, pending = wait(pending, timeout=stall_s, return_when=FIRST_COMPLETED)
                if not fin:
                    print(f'  raw read stalled (no block in {stall_s} s); restarting the workers')
                    break
                for fu in fin:
                    try:
                        a, val = fu.result()
                        done[a] = val
                        if log:
                            with open(log, 'a') as fh:
                                fh.write(f'{a} ')
                    except Exception as e:  # noqa: BLE001 - network errors take many forms
                        if 'Earthdata Login' in str(e):
                            die(str(e))
                        print(f'  block at line {futs[fu][2]} failed ({str(e)[:100]}); will retry')
                if len(done) % 10 == 0:
                    el = (dt.datetime.now() - t0).total_seconds()
                    print(f'  {len(done)}/{len(jobs)} blocks, {el:.0f} s', flush=True)
        finally:
            for p in list(getattr(ex, '_processes', {}).values()):
                p.terminate()
            ex.shutdown(wait=False, cancel_futures=True)
        todo = [j for j in jobs if j[2] not in done]
    if todo:
        die(f'{len(todo)} of {len(jobs)} raw-data blocks could not be read after 3 attempts (network); try again later')
    return done


def raw_window(name, url, m, l0, l1, c0, c1, workers):
    """Cache the BFPQ codes of lines l0..l1, samples c0..c1 (about 1.4 GB for the pulse window)."""
    key = cache_path(f'rawwin|{name}|{m["path"]}|{l0}|{l1}|{c0}|{c1}')[:-4] + '.npy'
    log = key[:-4] + '.blocks'
    shape = (l1 - l0, c1 - c0, 2)
    try:
        ok = np.load(key, mmap_mode='r').shape == shape
    except (OSError, ValueError):
        ok = False
    if not ok:   # (re)create the file; completion records of an earlier file don't apply to it
        mm = np.lib.format.open_memmap(key, 'w+', np.uint16, shape)
        del mm
        if os.path.exists(log):
            os.remove(log)
    have = set(map(int, open(log).read().split())) if os.path.exists(log) else set()
    jobs = [(url, m['path'], a, b, c0, c1, key, l0, None, None) for a, b in _line_blocks(l0, l1) if a not in have]
    if jobs:
        print(f'reading {l1 - l0} lines x {c1 - c0} samples of raw echoes ({(l1 - l0) * (c1 - c0) * 4 / 1e9:.1f} GB) '
              f'with {workers} workers; this takes several minutes')
        raw_blocks(jobs, workers, log)
    return np.load(key, mmap_mode='r')


def _codes(z, lut):
    return lut[z[..., 0]] + 1j * lut[z[..., 1]]


def _band(nc, fs, f_off, bw):
    """Mix f_off to zero and keep |f| <= bw/2: (mixer, mask) for an FFT along fast time."""
    return np.exp(-2j * np.pi * f_off * np.arange(nc) / fs), np.abs(np.fft.fftfreq(nc, 1 / fs)) <= bw / 2


def _bandpass(x, mix, keep):
    Y = np.fft.fft(x * mix, axis=-1)
    Y[..., ~keep] = 0
    return np.fft.ifft(Y, axis=-1)


def raw_lines(raw, lut, fs, fc, nmax=3):
    """Strongest narrowband emitters in a raw window: (centre Hz, width Hz, excess dB), strongest first."""
    LB, NF = 256, 1024
    nl, nc = raw.shape[:2]
    spec = []
    for b in range(nl // LB):
        x = _codes(raw[b * LB:(b + 1) * LB], lut)[:, :nc // NF * NF].reshape(LB, -1, NF)
        spec.append((np.abs(np.fft.fftshift(np.fft.fft(x, axis=-1), axes=-1)) ** 2).mean(axis=(0, 1)))
    S = np.array(spec)
    S /= np.median(S, axis=1, keepdims=True)
    f = fc + (np.arange(NF) - NF // 2) * fs / NF
    ex = np.percentile(S, 90, axis=0)
    ex[np.abs(f - fc) >= 0.42 * fs] = 0   # stay inside the passband
    k = np.convolve(ex, np.ones(5) / 5, 'same')
    out = []
    for _ in range(nmax):
        i = int(np.argmax(ex))
        if ex[i] < 2:
            break
        thr, lo, hi = k[i] / 10, i, i
        while lo > 0 and k[lo] > thr:
            lo -= 1
        while hi < NF - 1 and k[hi] > thr:
            hi += 1
        out.append((0.5 * (f[lo] + f[hi]), max(f[hi] - f[lo], 0.3e6), float(10 * np.log10(ex[i]))))
        ex[max(0, lo - 2):hi + 3] = 0
    return out


def raw_pulses(raw, lut, m, l0, c0, f_off, bw, thr=5.0):
    """Complete pulses of the emitter inside the window: detect on/off segments of the band-limited
    envelope, take the commonest pulse length as the emitter's long pulse, then measure each pulse's
    interpolated rising edge, received time, strength over the line's median and whether it clipped."""
    from scipy.ndimage import uniform_filter1d
    fs = _sample_rate(m)
    nl, nc = raw.shape[:2]
    mix, keep = _band(nc, fs, f_off, bw + 4e5)
    seg = []
    for a in range(0, nl, 1024):
        y = _bandpass(_codes(raw[a:a + 1024], lut), mix, keep)
        env = uniform_filter1d(np.abs(y), max(1, round(0.5e-6 * fs)), axis=1)
        noise = np.median(env, axis=1)
        for j in range(len(y)):
            e = env[j]
            on = e > max(thr * noise[j], 0.15 * e.max())
            if not on.any():
                continue
            d = np.diff(np.r_[0, on.astype(int), 0])
            for s, t_ in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
                if not (10e-6 <= (t_ - s) / fs <= 400e-6) or s < 5 or t_ > nc - 5:
                    continue
                hm = 0.5 * np.median(e[s:t_])   # tighten the gate to the flat top
                s2 = s + np.argmax(e[s:t_] > hm)
                t2 = s + (t_ - s) - np.argmax(e[s:t_][::-1] > hm)
                seg.append((a + j, s2, t2))
    if not seg:
        return None, np.zeros(0)
    seg = np.array(seg)
    dur = (seg[:, 2] - seg[:, 1]) / fs * 1e6
    # Complete pulses pile up at one length; fragments (pulses cut by fading or by the gate) are shorter
    # and spread out. Take the longest tight cluster as the emitter's long pulse.
    h, edges = np.histogram(dur, bins=np.arange(10, 401, 1.0))
    sc = np.convolve(h, np.ones(3), 'same')
    ok = (sc >= max(5, 0.02 * len(dur))) & (sc == ndimage.maximum_filter1d(sc, 7))
    if not ok.any():
        return None, dur
    i = np.where(ok)[0][-1]
    tp = float(np.median(dur[np.abs(dur - edges[i] - 0.5) <= 2]))
    seg = seg[(dur > 0.9 * tp) & (dur < 1.1 * tp)]
    Tp = tp * 1e-6
    rows, peaks = [], []
    for line, s, e in seg:
        if s < 60 or e + 60 > nc:
            continue
        z = raw[line]
        y = _bandpass(_codes(z, lut), mix, keep)
        env = uniform_filter1d(np.abs(y), 6)
        top = np.median(env[s:e])
        hm = 0.5 * top
        i = s - 48 + np.argmax(env[s - 48:s + 48] > hm)
        tr = (i - 1) + (hm - env[i - 1]) / (env[i] - env[i - 1]) if env[i] != env[i - 1] else float(i)
        tc = tr + Tp * fs / 2
        a0, a1 = int(tr + 3e-6 * fs), int(tr + (Tp - 3e-6) * fs)
        if a0 < 0 or a1 > nc:
            continue
        g, tt = y[a0:a1], (np.arange(a0, a1) - tc) / fs
        ph, w = np.unwrap(np.angle(g)), np.abs(g) ** 2
        A = np.vstack([np.ones_like(tt), tt, tt ** 2]).T
        coef, *_ = np.linalg.lstsq(A * np.sqrt(w)[:, None], ph * np.sqrt(w), rcond=None)
        phrms = np.sqrt(np.average((ph - A @ coef) ** 2, weights=w))
        q = np.abs(lut[z[int(tr):int(tr + Tp * fs)]]).max(axis=1)
        peaks.append((q.max(), np.mean(q >= 0.999 * q.max())))
        rows.append((line + l0, m['utc'][line + l0] + 2 * m['sr'][c0] / LIGHT + tc / fs, tr / fs * 1e6,
                     20 * np.log10(top / np.median(env)), phrms, coef[2] / np.pi))
    if not rows:
        return None, dur
    r, pk = np.array(rows), np.array(peaks)
    p = dict(line=r[:, 0].astype(int), t=r[:, 1], tr_us=r[:, 2], snr=r[:, 3], phrms=r[:, 4], K=r[:, 5], tp_us=tp)
    # Receiver clipping: strong pulses saturate at one code magnitude, so many of their samples sit at the
    # largest value seen. Without clipping only a sample or two reach it, and nothing is flagged.
    p['clipfrac'] = np.where(pk[:, 0] >= 0.999 * pk[:, 0].max(), pk[:, 1], 0.0)
    top20 = np.sort(p['clipfrac'])[-min(len(r), 20):]
    p['clipped'] = (p['clipfrac'] > 0.001) if top20.mean() > 0.002 else np.zeros(len(r), bool)
    return p, dur


def _pooled(pool):
    """Common beam shape, free gain per pulse: log P_i(r) = a_i + b1 r + b2 r^2 (fixed effects), with
    3-sigma clipping of residuals and per-pulse intercepts refitted on the kept samples. Returns
    (vertex range m, coefficients, pulses used)."""
    n = len(pool)
    g = np.repeat(np.arange(n), [len(q[0]) for q in pool])   # pulse of each sample
    r, lp = np.concatenate([q[0] for q in pool]), np.concatenate([q[1] for q in pool])
    r0 = np.median(r)
    r = (r - r0) / 1e3
    r2 = r ** 2
    mean = lambda v, k: (np.bincount(g, v * k, n) / np.maximum(np.bincount(g, k, n), 1))[g]   # per-pulse mean over k
    keep = np.ones(len(r), bool)
    b = np.array([0.0, -1.0])
    for _ in range(5):
        k = keep & (np.bincount(g, keep, n) >= 3)[g]
        if not k.any():
            break
        X = np.c_[r - mean(r, k), r2 - mean(r2, k)][k]
        b, *_ = np.linalg.lstsq(X, (lp - mean(lp, k))[k], rcond=None)
        res = lp - b[0] * r - b[1] * r2
        res -= mean(res, keep)
        s = 1.4826 * np.median(np.abs(res[keep]))
        keep = np.abs(res) < 3 * s
    return r0 - b[0] / (2 * b[1]) * 1e3, b, int((np.bincount(g, keep, n) >= 3).sum())


def _halfwidth_km(b):
    """Half-power half-width of a fitted beam shape (log power quadratic in km)."""
    return float(np.sqrt(np.log(2) / -b[1])) if b[1] < 0 else float('nan')


def _span(v, i, thr):
    """lo, hi: the run of v around index i that stays above thr."""
    lo = hi = i
    while lo > 0 and v[lo - 1] > thr:
        lo -= 1
    while hi < len(v) - 1 and v[hi + 1] > thr:
        hi += 1
    return lo, hi


def raw_pulse_cues(raw, lut, m, l0, c0, f_off, bw, p, sel):
    """Range cue (pooled beam-shape fit over the selected pulses) and beam-crossing time (azimuth
    envelope of the emitter's power over every line)."""
    from scipy.ndimage import uniform_filter1d
    fs = _sample_rate(m)
    nl, nc = raw.shape[:2]
    mix, keep = _band(nc, fs, f_off, bw)
    srw = m['sr'][c0:c0 + nc]
    Tp, st, sm = p['tp_us'] * 1e-6, max(1, round(1e-6 * fs)), max(1, round(2e-6 * fs))
    pool = []
    for line, tr, t in zip(p['line'][sel], p['tr_us'][sel], p['t'][sel]):
        y = _bandpass(_codes(raw[line - l0], lut), mix, keep)
        a0, a1 = int(tr * 1e-6 * fs + 4e-6 * fs), int(tr * 1e-6 * fs + (Tp - 4e-6) * fs)
        e = uniform_filter1d(np.abs(y) ** 2, sm)[a0:a1]
        pool.append((srw[a0:a1:st].copy(), np.log(e[::st] + 1e-30), t))
    if len(pool) < 20:
        return None
    R, b, npul = _pooled(pool)
    rng = np.random.default_rng(0)
    mad = lambda v: 1.4826 * np.median(np.abs(v - np.median(v)))
    se_p = mad(np.array([_pooled([pool[i] for i in rng.integers(0, len(pool), len(pool))])[0] for _ in range(200)]))
    Tq = np.array([q[2] for q in pool])
    blk = np.floor((Tq - Tq.min()) / 1.0).astype(int)   # 1 s blocks share antenna and geometry state
    ub = np.unique(blk)
    bsb = []
    for _ in range(200):
        sel_b = [pool[i] for b_ in rng.choice(ub, len(ub)) for i in np.where(blk == b_)[0]]
        bsb.append(_pooled(sel_b)[0])
    se_b = mad(np.array(bsb))
    o = np.argsort(Tq)
    h1, h2 = _pooled([pool[i] for i in o[:len(o) // 2]])[0], _pooled([pool[i] for i in o[len(o) // 2:]])[0]
    # azimuth envelope: per 0.2 s, 95th percentile of each line's peak in-band power
    utc = m['utc'][l0:l0 + nl]
    pw, pt = [], []
    for a in range(0, nl, 1024):
        y = _bandpass(_codes(raw[a:a + 1024], lut), mix, keep)
        pw.append(uniform_filter1d(np.abs(y) ** 2, st, axis=1).max(axis=1))
        pt.append(utc[a:a + len(y)])
    pw, pt = np.concatenate(pw), np.concatenate(pt)
    bins = np.arange(pt.min(), pt.max(), 0.2)
    idx = np.digitize(pt, bins)
    q = np.array([np.percentile(pw[idx == i], 95) if np.sum(idx == i) > 50 else np.nan for i in range(1, len(bins))])
    tb_, qd = bins[:-1] + 0.1, 10 * np.log10(q / np.median(pw))
    span = tb_[qd > np.nanmax(qd) - 6]
    tmid = 0.5 * (span.min() + span.max())
    exc = np.clip(10 ** (qd / 10) - 10 ** (np.nanmedian(qd) / 10), 0, None)
    exc[~np.isfinite(exc)] = 0
    tcen = np.sum(tb_ * exc) / np.sum(exc)
    return dict(R=float(R), Rse=float(max(se_p, se_b)), R_half1=float(h1), R_half2=float(h2),
                tR=float(np.median(Tq)), npul=int(npul), tb=float(0.5 * (tmid + tcen)), tbse=float(max(abs(tmid - tcen) / 2, 0.15)),
                az_t=tb_, az_db=qd, edge=bool(span.min() < tb_[2] or span.max() > tb_[-3] or not srw[0] + 2e3 < R < srw[-1] - 2e3),
                hump_halfwidth_km=_halfwidth_km(b))


def raw_noise_cues(prof, lines, m, t_win, r_win):
    """Cues for a continuous (noise-like) emitter from per-block mean power profiles: excess over a
    per-sample background, source pick inside the window, azimuth from the excess time series, range
    from a pooled quadratic over blocks within 3 dB of the azimuth peak."""
    P = prof.astype(float)
    t = m['utc'][np.minimum(lines + RAW_CHUNK // 2, len(m['utc']) - 1)]
    sr = m['sr'][:P.shape[1]]
    E = np.clip(P - np.percentile(P, 20, axis=0), 0, None)
    k = max(1, int(round(1000.0 / (sr[1] - sr[0]))))   # ~1 km in range
    Es = ndimage.uniform_filter1d(E, k, axis=1)
    mask = ((t >= t_win[0]) & (t <= t_win[1]))[:, None] & ((sr >= r_win[0]) & (sr <= r_win[1]))[None, :]
    i, j = np.unravel_index(np.argmax(np.where(mask, Es, 0)), Es.shape)
    a = E[:, np.abs(sr - sr[j]) < 10e3].mean(axis=1)
    if not np.isfinite(a).all() or a.max() <= 0:
        return None   # no excess power: nothing to locate
    ad = 10 * np.log10(a / a.max() + 1e-9)
    k0, k1 = _span(ad, i, -6)
    tmid = 0.5 * (t[k0] + t[k1])
    s_ = slice(max(0, k0 - 3), min(len(a), k1 + 4))
    tcen = np.sum(t[s_] * a[s_]) / np.sum(a[s_])
    blk = np.where(ad > -3)[0]   # blocks near the azimuth peak
    blk = blk[(blk >= k0) & (blk <= k1)]
    if len(blk) < 3:
        return None
    cp = np.polyfit(t[blk] - t[i], ad[blk], 2)
    tpar = t[i] - cp[1] / (2 * cp[0]) if cp[0] < 0 else np.nan
    tb, tbse = float(np.nanmedian([tmid, tcen, tpar])), float(max(np.nanstd([tmid, tcen, tpar]), 0.1))
    pr = Es[blk].mean(axis=0)
    c0, c1 = _span(10 * np.log10(pr / pr[j] + 1e-12), j, -6)
    cols = np.arange(c0, c1 + 1, 16)
    pool = [(sr[cols], np.log(Es[b_, cols] + 1e-12), t[b_]) for b_ in blk]
    R, b, _ = _pooled(pool)
    rng = np.random.default_rng(0)
    bs = [_pooled([pool[q] for q in rng.integers(0, len(pool), len(pool))])[0] for _ in range(200)]
    med = np.median(P, axis=0)   # samples outside the recorded swath are empty
    v = np.where(med > 0.1 * np.median(med[med > 0]))[0]
    edge = (sr[c0] < sr[v[0]] + 2e3 or sr[c1] > sr[v[-1]] - 2e3     # hump cut by the swath edge: range cue biased
            or k0 == 0 or k1 == len(ad) - 1)                           # envelope cut by the start or end of the read
    return dict(R=float(R), Rse=float(max(np.std(bs), 100.0)), tR=float(np.median(t[blk])), npul=int(len(blk)), tb=tb, tbse=tbse,
                az_t=t, az_db=ad, edge=bool(edge), fit_span_km=(float(sr[c0] / 1e3), float(sr[c1] / 1e3)),
                hump_halfwidth_km=_halfwidth_km(b))


def dem_height(lat, lon):
    """Ellipsoidal height of the ground: Copernicus GLO-30 (EGM2008 heights) plus the EGM2008 geoid.
    Sea or no tile: 0 m above the geoid."""
    import rasterio
    from pyproj import network
    a, b = math.floor(lat), math.floor(lon)
    name = f"Copernicus_DSM_COG_10_{'N' if a >= 0 else 'S'}{abs(a):02d}_00_{'E' if b >= 0 else 'W'}{abs(b):03d}_00_DEM"
    try:
        with rasterio.open(f'https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif') as d:
            h = max(float(next(d.sample([(lon, lat)]))[0]), 0.0)
    except Exception:  # noqa: BLE001 - no tile over open sea
        h = 0.0
    network.set_network_enabled(True)
    n = Transformer.from_crs('EPSG:4326+3855', 'EPSG:4979', always_xy=True).transform(lon, lat, 0.0)[2]
    return h + (n if math.isfinite(n) else 0.0)


class RawPass:
    """One pass's cues and geometry for the fix: slant range R at time tR, beam crossing at tb,
    Doppler centroid fdc at the radar's centre frequency fc. sd_R, sd_t: the spreads the pass is
    weighted and perturbed by (the validated ones, or the cue's own if larger)."""
    def __init__(self, m, cues, dc, bias):
        self.o, self.fc, self.left = RawOrbit(m), m['fc'], bool(m['left'])
        self.R, self.tR, self.tb = cues['R'] + bias, cues['tR'], cues['tb']
        self.sd_R, self.sd_t = max(RAW_SD_R, cues['Rse']), max(RAW_SD_T, cues['tbse'])
        if dc is None:
            self.fdc, self.fdc_src = FDC_NOMINAL, f'nominal {FDC_NOMINAL:.0f} Hz (no RSLC)'
        else:
            f = RegularGridInterpolator((dc['zt'], dc['sr']), dc['dc'], bounds_error=False, fill_value=None)
            self.fdc, self.fdc_src = float(f([[self.tb, cues['R']]])[0]), 'RSLC Doppler centroid table'

    def resid(self, x, R=None, tb=None, sr=1e3, st=1.0):
        """Range residual in units of sr metres and Doppler residual in units of st seconds of beam time."""
        _, rd = self.o.rdot(self.tb if tb is None else tb, x)
        fd = -2 * self.fc * rd / LIGHT - self.fdc
        rate = 2 * self.fc * np.linalg.norm(self.o.v(self.tb)) ** 2 / (LIGHT * self.R)   # Hz per s, roughly
        return [(np.linalg.norm(self.o.p(self.tR) - x) - (self.R if R is None else R)) / sr, fd / (rate * st)]

    def start(self):
        p, v = self.o.p(self.tb), self.o.v(self.tb)
        up = p / np.linalg.norm(p)
        side = np.cross(up, v / np.linalg.norm(v)) * (1 if self.left else -1)
        return [p - up * 700e3 + side * gr for gr in np.arange(200e3, 900e3, 50e3)], side, p


def raw_solve(passes, h, Rs=None, tbs=None, x0=None):
    """Ground point at ellipsoidal height h that best fits every pass's range and beam-crossing time."""
    from scipy.optimize import least_squares
    Rs, tbs = Rs or [None] * len(passes), tbs or [None] * len(passes)
    fun = lambda v: sum((p.resid(ecef(v[0], v[1], h), R, tb, p.sd_R, p.sd_t) for p, R, tb in zip(passes, Rs, tbs)), [])
    if x0 is not None:
        return least_squares(fun, x0, x_scale=[0.01, 0.01])
    best = None
    starts, side, p0 = passes[0].start()
    for xs in starts:
        la, lo, _ = llh(xs)
        s = least_squares(fun, [la, lo], x_scale=[0.01, 0.01])
        if np.dot(ecef(*s.x, h) - p0, side) > 0 and (best is None or s.cost < best.cost):
            best = s
    return best


def raw_fix(passes, x0=None):
    """Fix with the height iterated from the DEM (damped, to avoid 2-cycles on slopes), then the
    uncertainty by Monte Carlo over the validated cue spreads (or the cue's own, if larger)."""
    h, conv, s = 0.0, False, None
    for it in range(10):
        s = raw_solve(passes, h, x0=s.x if s is not None else x0)
        h2 = dem_height(*s.x)
        if abs(h2 - h) < 5:
            h, conv = h2, True
            break
        h = h2 if it == 0 else 0.5 * (h + h2)
    s = raw_solve(passes, h, x0=s.x)
    rng = np.random.default_rng(1)
    pts = []
    for _ in range(150):
        Rs = [p.R + rng.normal(0, p.sd_R) for p in passes]
        tbs = [p.tb + rng.normal(0, p.sd_t) for p in passes]
        pts.append(raw_solve(passes, h, Rs, tbs, x0=s.x).x)
    pts = np.array(pts)
    az, _, d = _GEOD.inv(np.full(len(pts), s.x[1]), np.full(len(pts), s.x[0]), pts[:, 1], pts[:, 0])
    e, n = d * np.sin(np.radians(az)), d * np.cos(np.radians(az))
    ev, V = np.linalg.eigh(np.cov(np.vstack([e, n])))
    x = ecef(*s.x, h)
    res = [p.resid(x) for p in passes]
    return dict(lat=round(float(s.x[0]), 5), lon=round(float(s.x[1]), 5), h_m=round(h), height_converged=conv,
                ellipse_1sigma_km=[round(float(np.sqrt(ev[1])) / 1e3, 2), round(float(np.sqrt(ev[0])) / 1e3, 2)],
                ellipse_major_bearing_deg=round(float(np.degrees(np.arctan2(V[0, 1], V[1, 1])) % 180)),
                r95_km=round(float(np.percentile(np.hypot(e, n), 95)) / 1e3, 2),
                residual_range_km=[round(float(r[0]), 2) for r in res], residual_time_s=[round(float(r[1]), 2) for r in res])


def _crossing(m, o, x0, fdc):
    """When the beam crosses x0: (time grid, slant ranges on it, index of the crossing, pulse line)."""
    t = np.linspace(m['utc'][0], m['utc'][-1], 40001)
    R0, rd = o.rdot(t, x0)
    ib = int(np.argmin(np.abs(-2 * m['fc'] * rd / LIGHT - fdc)))
    return t, R0, ib, int(np.searchsorted(m['utc'], t[ib]))


def raw_choose(g, a):
    """The raw granule of a pass (the one with most data either side of the beam crossing of the prior),
    its metadata and Doppler table, and the line and slant range where the beam crosses the prior."""
    pol = frame_pols(g)[0]
    x0, best = ecef(a.lat, a.lon, a.height), None
    for rname, url in find_rrsd(g):
        m = raw_meta(rname, url, pol)
        if m['utc'][0] < m['ot'][0] or m['utc'][-1] > m['ot'][-1]:
            die(f'{rname}: the orbit record does not cover the pulse times')
        o = RawOrbit(m)
        t, _, ib, c = _crossing(m, o, x0, FDC_NOMINAL)
        margin = min(c, len(m['utc']) - c)
        if 0 < ib < len(t) - 1 and (best is None or margin > best[0]):
            best = (margin, rname, url, m, o)
    if best is None:
        die(f'{g}: the beam does not cross {a.lat}, {a.lon} during the raw granule(s) of this pass')
    _, rname, url, m, o = best
    dc = raw_dc(g, m)
    t, R0, ib, c = _crossing(m, o, x0, FDC_NOMINAL if dc is None else float(np.nanmedian(dc['dc'])))
    if ib in (0, len(t) - 1):
        die(f'{rname}: the beam does not cross {a.lat}, {a.lon} during this raw granule')
    k = int(np.searchsorted(m['sr'], R0[ib]))
    if k <= 0 or k >= len(m['sr']) - 1:
        die(f'{rname}: {a.lat}, {a.lon} is outside the swath of this pass (slant range {R0[ib] / 1e3:.0f} km)')
    return dict(granule=g, rrsd=rname, url=url, pol=pol, m=m, dc=dc, line=c, sample=k, tb0=float(t[ib]), R0=float(R0[ib]))


def _pulse_cues(ch, a):
    """Pulse-mode cues from the raw window around the crossing: (fields for the pass, or None, and why not)."""
    m, c, k = ch['m'], ch['line'], ch['sample']
    L0, L1 = max(0, c - RAW_LINES), min(len(m['utc']), c + RAW_LINES)
    C0 = max(0, k - RAW_COLS // 2)
    C0 -= C0 % RAW_CHUNK
    C1 = min(m['ncol'], C0 + RAW_COLS)
    raw, lut = raw_window(ch['rrsd'], ch['url'], m, L0, L1, C0, C1, a.workers), m['lut']
    if a.freq_mhz:
        lines = [(a.freq_mhz * 1e6, a.bw_mhz * 1e6, float('nan'))]
    else:
        lines = raw_lines(raw, lut, _sample_rate(m), m['fc'])
        for f0, bw, x in lines:
            print(f'  narrowband line {f0 / 1e6:.2f} MHz, {bw / 1e6:.2f} MHz wide, {x:.1f} dB over the band median')
    if not lines:
        return None, 'no narrowband emitter in the raw window around the prior'
    f0, bw, _ = lines[0]
    p, _ = raw_pulses(raw, lut, m, L0, C0, f0 - m['fc'], bw)
    npulse = 0 if p is None else len(p['line'])
    why = f'no usable pulses at {f0 / 1e6:.2f} MHz near the prior (found {npulse})'
    if npulse < 30:
        return None, why
    clipped, sel, rule = p['clipped'], np.ones(npulse, bool), 'all complete pulses (non-linear sweep)'
    if np.median(p['phrms']) < 0.6:   # linear chirps (ARSR-4): drop pulses the chirp fit says are corrupted
        sel, rule = p['phrms'] < 0.6, 'pulses that fit a linear chirp'
    print(f"  {npulse} complete pulses of ~{p['tp_us']:.0f} us at {f0 / 1e6:.2f} MHz ({clipped.sum()} clipped); "
          f'range cue from {sel.sum()}: {rule}')
    cues = raw_pulse_cues(raw, lut, m, L0, C0, f0 - m['fc'], bw, p, sel)
    if cues is None:
        return None, why
    return dict(kind='pulse', cues=cues, emitter_MHz=round(f0 / 1e6, 2), width_MHz=round(bw / 1e6, 2),
                pulse_us=round(p['tp_us'], 1), pulses=int(npulse), pulses_used=int(sel.sum()), pulses_clipped=int(clipped.sum()),
                sweep_kHz_per_us=round(float(np.median(p['K'][sel])) * 1e-9, 1),
                chirp_phase_rms_rad=round(float(np.median(p['phrms'])), 2), pulse_rule=rule), None


def raw_pass(ch, a):
    """Cues for one pass (from raw_choose): pulse mode on the window around the prior, else noise mode."""
    m, rname, c = ch['m'], ch['rrsd'], ch['line']
    print(f"{ch['granule']}\n  raw data: {rname} ({ch['pol']})")
    out = dict(granule=ch['granule'], rrsd=rname, pol=ch['pol'], fc_MHz=m['fc'] / 1e6, dc=ch['dc'], m=m)
    if a.kind != 'noise':
        got, why = _pulse_cues(ch, a)
        if got:
            return {**out, **got}
        if a.kind == 'pulse':
            die(f'{rname}: {why}; try --kind noise or --freq-mhz')
        print('  no pulsed emitter found: treating it as continuous (noise-like) interference')
    # noise mode: mean power profile per chunk of lines across the whole swath
    L0, L1 = max(0, c - NOISE_LINES), min(len(m['utc']), c + NOISE_LINES)
    band = (a.freq_mhz * 1e6 - m['fc'], a.bw_mhz * 1e6, _sample_rate(m)) if a.freq_mhz else None   # else the whole recorded band
    key = cache_path(f"rawprof|{rname}|{ch['pol']}|{L0}|{L1}|{band}")
    if os.path.exists(key):
        z = np.load(key)
        lines_, prof = z['line'], z['prof']
    else:
        jobs = [(ch['url'], m['path'], s_, e_, 0, m['ncol'], None, L0, band, m['lut']) for s_, e_ in _line_blocks(L0, L1)]
        print(f'reading {L1 - L0} lines x {m["ncol"]} samples ({(L1 - L0) * m["ncol"] * 4 / 1e9:.0f} GB streamed, '
              f'not kept) with {a.workers} workers; this takes tens of minutes')
        got = raw_blocks(jobs, a.workers)
        lines_ = np.array(sorted(got))
        prof = np.array([got[s_] for s_ in lines_])
        np.savez(key, line=lines_, prof=prof)
    tb0, R0 = ch['tb0'], ch['R0']
    cues = raw_noise_cues(prof, lines_, m, (tb0 - 8, tb0 + 8), (R0 - 30e3, R0 + 30e3))
    if cues is None:
        die(f'{rname}: no emitter found near the prior (no pulses, and no excess power in the swath)')
    return {**out, 'kind': 'noise', 'cues': cues}


def cmd_rawfix(a):
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    bias = 0.0 if a.no_bias_correction else RAW_BIAS_M
    chosen = []
    for g in a.granule:
        ch = raw_choose(g, a)
        if any(ch['rrsd'] == q['rrsd'] for q in chosen):   # another product (or frame) of an acquisition already in
            print(f"{g}\n  same raw acquisition as an earlier granule: left out ({ch['rrsd']})")
            continue
        chosen.append(ch)
    runs = [raw_pass(ch, a) for ch in chosen]
    res = {'prior': [a.lat, a.lon], 'range_bias_correction_m_pulse_passes': bias, 'passes': []}
    passes = []
    for r in runs:
        c, n = r['cues'], parse_name(r['granule'])
        b = bias if r['kind'] == 'pulse' else 0.0   # the correction was measured on pulsed radars only
        p = RawPass(r['m'], c, r['dc'], b)
        passes.append(p)
        f1 = raw_fix([p])
        f0 = raw_fix([RawPass(r['m'], c, r['dc'], 0.0)], x0=[f1['lat'], f1['lon']]) if b else f1
        warn = []
        if c['edge']:
            warn.append('the emitter hump or envelope reaches the edge of the data read: the cue may be biased')
        if 'R_half1' in c and abs(c['R_half1'] - c['R_half2']) > 3e3:
            warn.append(f"split halves disagree by {abs(c['R_half1'] - c['R_half2']) / 1e3:.1f} km: range cue unstable")
        if c['Rse'] > 3e3:
            warn.append(f"range cue uncertainty {c['Rse'] / 1e3:.1f} km: fit unreliable")
        if not f1['height_converged']:
            warn.append('height iteration did not converge')
        if r['kind'] == 'noise':
            warn.append('continuous-interference mode: accuracy not tested against a known emitter')
        _, _, dprior = _GEOD.inv(a.lon, a.lat, f1['lon'], f1['lat'])
        row = {k: v for k, v in r.items() if k not in ('m', 'dc', 'cues')}
        row.update(time=f"{n['start']:%Y-%m-%dT%H:%MZ}", direction=n['dir'], track=n['track'], doppler_centroid=p.fdc_src,
                   fdc_Hz=round(p.fdc, 1), range_bias_correction_m=b, slant_range_km=round(c['R'] / 1e3, 3), slant_range_se_km=round(c['Rse'] / 1e3, 2),
                   beam_crossing_time_se_s=round(c['tbse'], 2), cue_count=c['npul'],
                   hump_halfwidth_km=round(c['hump_halfwidth_km'], 1), fix=f1, fix_as_measured=f0,
                   km_from_prior=round(dprior / 1e3, 1), warnings=warn)
        if 'R_half1' in c:
            row['split_half_ranges_km'] = [round(c['R_half1'] / 1e3, 2), round(c['R_half2'] / 1e3, 2)]
        res['passes'].append(row)
        print(f"  {r['kind']} cue: slant range {c['R'] / 1e3:.2f} +- {c['Rse'] / 1e3:.2f} km, beam crossing +- {c['tbse']:.2f} s, f_dc {p.fdc:.0f} Hz ({p.fdc_src})")
        print(f"  fix {f1['lat']:.4f}, {f1['lon']:.4f} (h {f1['h_m']} m); 95% of draws over the validated cue spreads within {f1['r95_km']:.1f} km; "
              f"{dprior / 1e3:.1f} km from the prior")
        for w in warn:
            print(f'  WARNING: {w}')
    if len(passes) > 1:
        j = raw_fix(passes, x0=[res['passes'][0]['fix']['lat'], res['passes'][0]['fix']['lon']])
        res['joint'] = j
        print(f"joint fix, {len(passes)} passes: {j['lat']:.4f}, {j['lon']:.4f}; 95% of draws within {j['r95_km']:.1f} km; "
              f"residuals range {j['residual_range_km']} km, beam time {j['residual_time_s']} s")
        if any(abs(rr) > 3 * p.sd_R / 1e3 or abs(rt) > 3 * p.sd_t
               for rr, rt, p in zip(j['residual_range_km'], j['residual_time_s'], passes)):
            print('  WARNING: some passes disagree by more than 3 times their spread: check them separately '
                  '(two emitters, or a cue gone wrong)')
    js = os.path.splitext(a.out)[0] + '.json'
    with open(js, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, indent=1, default=float)
    # figure: azimuth envelope per pass, and the fixes around the prior
    fig, ax = plt.subplots(1, 2, figsize=(14, 5.6))
    for r, row in zip(runs, res['passes']):
        c = r['cues']
        ax[0].plot(np.asarray(c['az_t']) - c['tb'], c['az_db'] - np.nanmax(c['az_db']), lw=1,
                   label=f"{row['time']} {row['direction'][:4]} ({r['kind']})")
    ax[0].axvline(0, color='k', lw=0.8)
    ax[0].set_xlabel('s from the estimated beam crossing'); ax[0].set_ylabel('emitter power, dB from peak')
    ax[0].set_ylim(-30, 2); ax[0].legend(fontsize=8); ax[0].set_title('azimuth envelopes', loc='left', fontsize=10)
    loc = Transformer.from_crs(4326, local_crs(a.lat, a.lon), always_xy=True)
    pts = [(row['fix'], row['time'] + ' ' + row['direction'][:4]) for row in res['passes']]
    if 'joint' in res:
        pts.append((res['joint'], 'joint'))
    for f_, lab in pts:
        e, nn = loc.transform(f_['lon'], f_['lat'])
        a1, a2 = f_['ellipse_1sigma_km']
        th = np.radians(90 - f_['ellipse_major_bearing_deg'])
        u = np.linspace(0, 2 * np.pi, 100)
        ex, ey = a1 * np.cos(u), a2 * np.sin(u)
        ln, = ax[1].plot(e / 1e3, nn / 1e3, 'P' if lab == 'joint' else 's', ms=9 if lab == 'joint' else 6, label=lab)
        ax[1].plot(e / 1e3 + ex * np.cos(th) - ey * np.sin(th), nn / 1e3 + ex * np.sin(th) + ey * np.cos(th), lw=0.8, color=ln.get_color())
    ax[1].plot(0, 0, '+', color='k', ms=14, mew=2, label='prior (search point)')
    ax[1].set_aspect('equal'); ax[1].legend(fontsize=8)
    ax[1].set_xlabel('km east of the prior'); ax[1].set_ylabel('km north')
    corr = f'pulse ranges +{bias / 1e3:.2f} km bias correction' if bias and any(r['kind'] == 'pulse' for r in runs) else 'no range correction'
    ax[1].set_title(f'fixes ({corr}), 1-sigma ellipses', loc='left', fontsize=10)
    fig.suptitle('NISAR raw-data emitter fix', x=0.01, ha='left')
    fig.text(0.01, 0.003, CREDIT, fontsize=8, color='#444')
    fig.tight_layout(rect=(0, 0.02, 1, 0.95))
    fig.savefig(a.out, dpi=95, facecolor='white')
    print('Accuracy: single-pass fixes with the range-bias correction were 0.1-2.7 km (median 2.0) from 8 known ARSR-4 radars; '
          'see references/validation.md. The prior only chooses what to read: it should be within ~10 km of slant range '
          'and a few seconds of beam time (an rfimeta hotspot or a blob is enough).')
    print(f'wrote {a.out} and {js}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scenes', help='NISAR passes over a point (no login needed)')
    s.add_argument('lat', type=float); s.add_argument('lon', type=float); s.add_argument('start'); s.add_argument('end')
    s.add_argument('--release', choices=['any', 'provisional', 'beta'], default='any')
    s.add_argument('--all-modes', action='store_true', help='include ocean/ice modes with no frequency A')
    s.set_defaults(fn=cmd_scenes)
    s = sub.add_parser('crop', help='north-up HH/HV images around a point (aircraft, ships, sites)')
    s.add_argument('lat', type=float); s.add_argument('lon', type=float); s.add_argument('size', type=float)
    s.add_argument('granule', nargs='+')
    s.add_argument('--gcov', action='store_true', help='use GCOV (multilooked backscatter) instead of GSLC (finest detail)')
    s.add_argument('--looks', type=float, default=10.0, help='GSLC averaging window in metres (default 10)')
    s.add_argument('--markers', help='CSV with lat and lon columns (optional label) to circle, for example an ADS-B track CSV')
    s.add_argument('--out', default='nisar_crop.png')
    s.set_defaults(fn=cmd_crop)
    s = sub.add_parser('blobs', help='radio-interference blobs near a point and their centres')
    s.add_argument('lat', type=float); s.add_argument('lon', type=float); s.add_argument('granule')
    s.add_argument('--size-km', type=float, default=120.0, help='side length of the search square in local east/north kilometres (default 120)')
    s.add_argument('--max', type=int, default=3)
    s.add_argument('--min-db', type=float, default=6.0)
    s.add_argument('--pol', choices=['HV', 'HH'], default='HV', help='HV = cross-pol (VH in VV/VH modes), HH = like-pol')
    s.add_argument('--out', default='nisar_blobs.png')
    s.set_defaults(fn=cmd_blobs)
    s = sub.add_parser('rfimeta', help="the RFI filter's record (RSLC metadata), mapped to the ground")
    s.add_argument('granule')
    s.add_argument('--pol', help="polarisation record to read (default: the mode's cross-pol, HV or VH)")
    s.add_argument('--height', type=float, default=0.0, help='assumed emitter height above the ellipsoid (m)')
    s.add_argument('--tone-frac', type=float, default=0.05, help='bins flagged in more than this share across the pass are treated as ever-present tones')
    s.add_argument('--out', default='nisar_rfimeta.png')
    s.set_defaults(fn=cmd_rfimeta)
    s = sub.add_parser('rawfix', help='locate an in-band emitter from the raw echoes (L0B RRSD); one pass or several')
    s.add_argument('lat', type=float, help='rough position (an rfimeta hotspot or blob): only chooses what to read')
    s.add_argument('lon', type=float)
    s.add_argument('granule', nargs='+', help='any product name of each pass; the raw granule is found from it')
    s.add_argument('--kind', choices=['auto', 'pulse', 'noise'], default='auto',
                   help='pulse: radars; noise: continuous interference such as jammers (reads the whole swath); auto: pulse if pulses are found')
    s.add_argument('--freq-mhz', type=float, help="emitter's centre frequency (default: the strongest narrowband line)")
    s.add_argument('--bw-mhz', type=float, default=1.5, help='band to keep around --freq-mhz (default 1.5)')
    s.add_argument('--height', type=float, default=0.0, help='height of the prior above the ellipsoid (m), for choosing the window')
    s.add_argument('--no-bias-correction', action='store_true', help='report the range cue as measured (default: +0.91 km, from validation)')
    s.add_argument('--workers', type=int, default=8, help='parallel readers (default 8)')
    s.add_argument('--out', default='nisar_rawfix.png')
    s.set_defaults(fn=cmd_rawfix)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
