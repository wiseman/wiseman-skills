#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pyproj", "matplotlib", "requests", "h5py", "earthaccess"]
# ///
"""NISAR (NASA-ISRO L-band radar) tools: find passes over a place, look at HH/HV imagery north-up
(parked aircraft, ships, anything else), find radio-interference blobs and their centres, and read
the interference record NISAR's RFI filter writes into each RSLC product.

  nisartool.py scenes LAT LON START END [--release any|provisional|beta] [--all-modes]
  nisartool.py crop LAT LON SIZE_M GRANULE [GRANULE ...] [--gcov] [--looks M] [--markers CSV] [--out PNG]
  nisartool.py blobs LAT LON GRANULE [--size-km 120] [--max 3] [--min-db 6] [--pol HV|HH] [--out PNG]
  nisartool.py rfimeta GRANULE [--pol HV] [--height M] [--tone-frac 0.05] [--out PNG]

GRANULE is a NISAR granule name such as
NISAR_L2_PR_GCOV_010_005_A_018_4005_DHDH_A_20260109T125114_20260109T125149_P05023_N_F_J_001 (from
`scenes`). Any of its products (GCOV, GSLC, RSLC) works: each command switches to the product it
needs for the same acquisition. Search needs no account. Reading data needs a free NASA Earthdata
Login: ~/.netrc with a urs.earthdata.nasa.gov entry, or $EARTHDATA_USERNAME/$EARTHDATA_PASSWORD, or
$EARTHDATA_TOKEN. Files are cloud-optimised HDF5 (1.5-26 GB each); the tool reads only the window it
needs over HTTPS and caches it in $NISARTOOL_CACHE (default ~/.cache/nisartool).

How accurate blob and RFI-record positions were in tests: references/validation.md.
"""

import argparse, csv, datetime as dt, hashlib, json, math, os, re, sys, tempfile, warnings

import numpy as np
import requests
from pyproj import CRS, Transformer
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
POSTING = {0: 'frequency B only, GCOV 80 m', 20: 'GSLC 10x5 m, GCOV 20 m', 40: 'GSLC 5x5 m, GCOV 10 m', 77: 'GSLC 2.5x5 m, GCOV 20 m'}
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
    (bandwidth of frequency A and B in MHz), polarisations, start time, release (from the CRID:
    X... = BETA, otherwise PROVISIONAL)."""
    p = g.split('_')
    if len(p) < 15 or p[0] != 'NISAR':
        die(f'{g}: not a NISAR granule name (they start NISAR_L2_PR_GCOV_...; get them from `scenes`)')
    mode = p[8]
    return {'product': p[3], 'track': p[5], 'dir': 'ascending' if p[6] == 'A' else 'descending',
            'frame': p[7], 'bw_a': int(mode[:2]), 'bw_b': int(mode[2:]), 'pols': p[9],
            'start': dt.datetime.strptime(p[11], '%Y%m%dT%H%M%S').replace(tzinfo=dt.timezone.utc),
            'release': 'beta' if p[13].startswith('X') else 'provisional'}


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
    """Power (float32) of `pols` in frequency A of a geocoded product (GCOV or GSLC) in a square of
    +-half_m around a point, optionally averaged into blocks of about block_m. Returns
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
    rows = []
    for e in cmr({'short_name[]': [SHORT[('GCOV', r)] for r in rels], 'point': f'{a.lon},{a.lat}', 'temporal': f'{start},{end}'}):
        if e['title'].startswith('NISAR_'):
            rows.append((e['title'], parse_name(e['title'])))
    rows.sort(key=lambda r: r[1]['start'])
    shown = 0
    for g, n in rows:
        if not a.all_modes and n['bw_a'] == 0:
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
    print(f'\n{shown} pass(es). BETA covers passes from Oct 2025 to Jan 2026 and is uncalibrated, with no RFI removed; '
          'PROVISIONAL (from mid-2026) is calibrated and RFI-filtered. Public browse image of a granule: '
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
    return sm - bg, found


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
    valid = np.isfinite(V)
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
    json.dump({'granule': g, 'lat': a.lat, 'lon': a.lon, 'size_km': a.size_km, 'channel': layer, 'blobs': res}, open(js, 'w'), indent=1)
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
    lam = 299_792_458.0 / float(f[fa(f'/science/LSAR/RSLC/swaths/frequency{freq}/processedCenterFrequency')][()])
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
            w = (~tones) / (~tones).sum()
            for p in parts:
                p['share'] = p['hc'] @ w   # share of flagged cells outside the ever-present tones
            all_share = np.concatenate([p['share'][:, 3:-3].ravel() for p in parts])
            med = float(np.median(all_share))
            sm_med = float(np.nanmedian(np.concatenate([p['sm'].ravel() for p in parts]))) if has_sm else 0.0
            flag, level = [], []
            for p in parts:
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
                    'flag_hotspots': top5(flag, flag_fields),
                    'level_hotspots': top5(level, lambda v, p, ti, ri: {'level_db_above_median': round(float(v), 1)})}
            top_spec = info['flag_hotspots'][0]['_spec'] if info['flag_hotspots'] else None
            for h in info['flag_hotspots']:
                del h['_spec']
            vmax_s = np.percentile(100 * all_share, 99.5)
            lev_max = max(10, np.nanpercentile(np.concatenate([p['lev'].ravel() for p in parts]), 99.5)) if has_sm else None
            for p in parts:
                T, R = p['t'] - t_first, p['r'] / 1e3
                m0 = ax[fi, 0].pcolormesh(T, R, 100 * p['share'].T, cmap='magma', vmin=0, vmax=vmax_s, shading='nearest')
                if has_sm:
                    m1 =ax[fi, 1].pcolormesh(T, R, p['lev'].T, cmap='viridis', vmin=-3, vmax=lev_max, shading='nearest')
            plt.colorbar(m0, ax=ax[fi, 0], label='% of cells flagged')
            ax[fi, 0].set_title(f'{fr}: share flagged (tones flagged across the whole pass excluded)', loc='left', fontsize=9)
            if has_sm:
                plt.colorbar(m1, ax=ax[fi, 1], label='dB above pass median')
                ax[fi, 1].set_title(f"{fr}: detector's typical level (signalMean)", loc='left', fontsize=9)
            for hs, c in ((info['flag_hotspots'], 'cyan'), (info['level_hotspots'], 'white')):
                for h in hs:
                    for axx in ax[fi, :2]:
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
    json.dump(out, open(js, 'w'), indent=1)
    print(f"{g} ({rel}{'; RFI mitigation: ' + str(mitig) if mitig else ''})")
    for fr, info in out['freqs'].items():
        print(f'frequency {fr} {pol}: tones flagged across the whole pass (MHz): {info["tones_everywhere_MHz"] or "none"}')
        for h in info['flag_hotspots']:
            print(f"  flag hotspot  t={h['time_s']:5.1f}s R={h['slant_km']:6.1f}km -> {h['lat']:.4f}, {h['lon']:.4f}  "
                  f"{100 * h['flagged_share']:.1f}% flagged, mostly {h['main_freqs_MHz']} MHz")
        for h in info['level_hotspots']:
            print(f"  level hotspot t={h['time_s']:5.1f}s R={h['slant_km']:6.1f}km -> {h['lat']:.4f}, {h['lon']:.4f}  "
                  f"+{h['level_db_above_median']:.1f} dB (persistent broadband interference raises this)")
    print('Ground positions are rough: the strongest hotspot was 11-24 km from 3 of 4 known radars and 97 km from the '
          'fourth, and weaker hotspots are often spurious. Use this to learn that an emitter was present and its frequency, '
          'not where it is; for position use `blobs` on BETA products, or crossing passes.')
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
    s.add_argument('--size-km', type=float, default=120.0)
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
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
