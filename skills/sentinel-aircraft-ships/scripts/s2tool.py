#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pyproj", "rasterio", "requests", "matplotlib"]
# ///
"""Sentinel-2 tools for aviation and maritime work: find scenes and the moment a
point was imaged, find an ADS-B-tracked aircraft in a scene (or identify which
aircraft made a streak), render crops for visual inspection (ships, anything),
and measure ship motion between two sightings.

  s2tool.py scenes LAT LON START END [--max-cloud 60]
  s2tool.py when ITEM LAT LON
  s2tool.py find ITEM (--hex HEX | --track CSV) [--out DIR]
  s2tool.py identify ITEM LAT LON [--radius-km 15] [--out DIR]
  s2tool.py crop LAT LON SIZE_M ITEM [ITEM ...] [--grid N] [--nir] [--out PNG]
  s2tool.py ships ITEM LAT LON SIZE_M [--compare ITEM2] [--top 24] [--out PNG]
  s2tool.py ship-motion ITEM1 LAT1 LON1 ITEM2 LAT2 LON2

ITEM is an Earth Search (Element84) Sentinel-2 L2A item id, e.g.
S2B_T30UXA_20261014T110010_L2A (collection sentinel-2-c1-l2a).
--track CSV columns: time (ISO 8601 or epoch s), lat, lon, alt_geom_ft, alt_baro_ft
(either altitude may be blank). --hex / identify fetch ADS-B from the Hexbird
HTTP API using $HEXBIRD_API_BASE (default https://hexbird.io) and $HEXBIRD_TOKEN.
Results print as text; find/identify also write JSON and an overlay PNG.
"""

import argparse, csv, json, math, os, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import requests
import rasterio
from pyproj import Transformer
from rasterio.windows import Window
from scipy.ndimage import label, map_coordinates, maximum_filter, median_filter

# Remote COGs: don't list the directory or probe for sidecar files (.aux.xml, .msk, ...)
# on every open: ~10 wasted HEAD requests per band.
os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif')

EARTH_SEARCH = 'https://earth-search.aws.element84.com/v1'
CDSE = 'https://catalogue.dataspace.copernicus.eu/odata/v1/Products'
COLLECTIONS = ['sentinel-2-c1-l2a', 'sentinel-2-l2a']
BANDS = ['B02', 'B08', 'B03', 'B04']
ASSET = {'B02': 'blue', 'B03': 'green', 'B04': 'red', 'B08': 'nir', 'SCL': 'scl'}
BAND_ID = {'B02': 1, 'B03': 2, 'B04': 3, 'B08': 7}
# MSI band acquisition lag after B02 (s) on odd-numbered detectors. Even-numbered
# detectors are mounted reversed: B04 first, same spacing.
LAG = {'B02': 0.0, 'B08': 0.264, 'B03': 0.527, 'B04': 1.005}
LAG_EVEN = {b: LAG['B04'] - v for b, v in LAG.items()}
# Imaging time of a point vs the tile's SENSING_TIME (fitted on 13 aircraft in
# 9 tiles, S2A/B/C): t = SENSING_TIME + 0.3 s - 0.126 s/km * km north of the
# tile centre; rms 2.4 s, worst 5.6 s. Descending (daytime) passes only.
T_OFFSET, T_SLOPE, T_RMS = 0.27, -0.126, 2.4
SEARCH_HALF = 12.0      # s searched either side of the model time
ZCAP, DETECT_Z, TOL_PX = 10.0, 4.0, 5
CROP_MAX_PX = 2000      # crops wider than 20 km are read downsampled
SHIPS_MAX_M = 40000     # largest box `ships` will scan
SCL_NAMES = {0: 'no data', 1: 'saturated', 2: 'dark/shadow', 3: 'cloud shadow', 4: 'vegetation', 5: 'bare soil',
             6: 'water', 7: 'unclassified', 8: 'cloud (medium)', 9: 'cloud (high)', 10: 'thin cirrus', 11: 'snow'}


def iso(t, ms=True):
    s = datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')
    return (s[:-4] if ms else s[:-7]) + 'Z'


def parse_time(s):
    try:
        return float(s)
    except ValueError:
        return datetime.fromisoformat(s.strip().replace('Z', '+00:00').replace(' ', 'T')).replace(tzinfo=timezone.utc).timestamp() \
            if 'T' in s or ' ' in s else float(s)


# ---------------------------------------------------------------- heights
_geoid = None


def geoid_m(lat, lon):
    """EGM2008 geoid height (m above the WGS84 ellipsoid), via PROJ's network grid."""
    global _geoid
    try:
        if _geoid is None:
            import pyproj
            pyproj.network.set_network_enabled(True)
            _geoid = Transformer.from_crs('EPSG:4326+3855', 'EPSG:4979', always_xy=True)
        n = _geoid.transform(lon, lat, 0.0)[2]
        return float(n) if math.isfinite(n) and abs(n) < 150 else 0.0
    except Exception:
        return 0.0


def dem_m(lat, lon):
    """Copernicus GLO-30 terrain height (m above EGM2008); 0 over open sea."""
    la, lo = math.floor(lat), math.floor(lon)
    name = f"Copernicus_DSM_COG_10_{'N' if la >= 0 else 'S'}{abs(la):02d}_00_{'E' if lo >= 0 else 'W'}{abs(lo):03d}_00_DEM"
    try:
        with rasterio.open(f'https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif') as src:
            v = float(next(src.sample([(lon, lat)]))[0])
            return v if math.isfinite(v) and v > -500 else 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------- scenes
class Scene:
    def __init__(self, item_id, item=None):
        self.item_id = item_id
        self.item = item  # a search result can be passed in to skip the item lookup
        for c in ([] if item else COLLECTIONS):
            r = requests.get(f'{EARTH_SEARCH}/collections/{c}/items/{item_id}', timeout=60)
            if r.ok:
                self.item = r.json(); break
        if self.item is None:
            sys.exit(f'item {item_id} not found in {COLLECTIONS}')
        x = urllib.request.urlopen(self.item['assets']['granule_metadata']['href'], timeout=60).read().decode()
        self.sensing = parse_time(re.search(r'<SENSING_TIME[^>]*>([^<]+)<', x).group(1))
        geo = re.search(r'<Geoposition resolution="10">\s*<ULX>([\d.]+)</ULX>\s*<ULY>([\d.]+)', x)
        self.ulx, self.uly = float(geo.group(1)), float(geo.group(2))
        self.epsg = int(re.search(r'<HORIZONTAL_CS_CODE>EPSG:(\d+)<', x).group(1))
        self.fwd = Transformer.from_crs(4326, self.epsg, always_xy=True)
        self.inv = Transformer.from_crs(self.epsg, 4326, always_xy=True)
        vals = lambda b: np.array([[float(v) for v in r.split()] for r in re.findall(r'<VALUES>([^<]+)</VALUES>', b)])
        self.angles = {}
        for band, bid in BAND_ID.items():
            g = []
            for det, blk in re.findall(rf'<Viewing_Incidence_Angles_Grids bandId="{bid}" detectorId="(\d+)">(.*?)</Viewing_Incidence_Angles_Grids>', x, re.S):
                g.append((int(det), vals(re.search(r'<Zenith>(.*?)</Zenith>', blk, re.S).group(1)), vals(re.search(r'<Azimuth>(.*?)</Azimuth>', blk, re.S).group(1))))
            self.angles[band] = g

    def utm(self, lat, lon):
        return self.fwd.transform(lon, lat)

    def latlon(self, x, y):
        lon, lat = self.inv.transform(x, y); return lat, lon

    def km_north_of_centre(self, y):
        return (y - (self.uly - 54900)) / 1000

    def model_time(self, y):
        return self.sensing + T_OFFSET + T_SLOPE * self.km_north_of_centre(y)

    def view(self, band, x, y):
        """(zenith, azimuth, detector) for a band at UTM x, y; azimuth points to the satellite."""
        c, r = (x - self.ulx) / 5000, (self.uly - y) / 5000
        c0, r0 = int(math.floor(c)), int(math.floor(r)); fc, fr = c - c0, r - r0
        for det, zen, az in self.angles[band]:
            if not (0 <= r0 < zen.shape[0] - 1 and 0 <= c0 < zen.shape[1] - 1):
                continue
            z4, a4 = zen[r0:r0 + 2, c0:c0 + 2], az[r0:r0 + 2, c0:c0 + 2]
            if np.isnan(z4).any():
                continue
            w = np.array([[(1 - fc) * (1 - fr), fc * (1 - fr)], [(1 - fc) * fr, fc * fr]])
            ax = (np.sin(np.radians(a4)) * w).sum(); ay = (np.cos(np.radians(a4)) * w).sum()
            return float((z4 * w).sum()), float(np.degrees(np.arctan2(ax, ay)) % 360), det
        return None

    def read(self, band, x0, y0, x1, y1, max_px=None):
        """Band array for a UTM box and its upper-left corner. With max_px, a
        bigger window is read from the file's overviews, downsampled so the
        longer side is at most max_px (fast; check the pixel size returned)."""
        with rasterio.open(self.item['assets'][ASSET[band]]['href']) as src:
            t = src.transform
            c0 = max(int(math.floor((x0 - t.c) / t.a)), 0); r0 = max(int(math.floor((t.f - y1) / -t.e)), 0)
            c1 = min(int(math.ceil((x1 - t.c) / t.a)), src.width); r1 = min(int(math.ceil((t.f - y0) / -t.e)), src.height)
            if c1 <= c0 or r1 <= r0:
                return np.zeros((0, 0)), x0, y1
            w, h = c1 - c0, r1 - r0
            if max_px and max(w, h) > max_px:
                k = max(w, h) / max_px
                shape = (max(1, int(h / k)), max(1, int(w / k)))
                a = src.read(1, window=Window(c0, r0, w, h), out_shape=shape, resampling=rasterio.enums.Resampling.average).astype(float)
            else:
                a = src.read(1, window=Window(c0, r0, w, h)).astype(float)
            return a, t.c + c0 * t.a, t.f + r0 * t.e

    def scl_at(self, x, y):
        a, ox, oy = self.read('SCL', x - 10, y - 10, x + 10, y + 10)
        return int(a[a.shape[0] // 2, a.shape[1] // 2]) if a.size else 0


def search_scenes(lat, lon, start, end, max_pages=10):
    """All L2A items over the point in [start, end], oldest first (follows STAC paging)."""
    body = {'collections': [COLLECTIONS[0]], 'intersects': {'type': 'Point', 'coordinates': [lon, lat]},
            'datetime': f'{start}T00:00:00Z/{end}T23:59:59Z', 'limit': 200}
    feats = []
    for _ in range(max_pages):
        r = requests.post(f'{EARTH_SEARCH}/search', json=body, timeout=60); r.raise_for_status()
        d = r.json(); feats += d['features']
        nxt = next((l for l in d.get('links', []) if l.get('rel') == 'next'), None)
        if len(d['features']) < body['limit'] or not nxt:  # Earth Search adds a next link even on the last page
            break
        body = {**body, **nxt['body']}
    return sorted(feats, key=lambda f: f['properties']['datetime'])


def product_key(name):
    """(satellite, datatake start, relative orbit, tile) from a product name, L1C or L2A, e.g.
    S2B_MSIL2A_20261014T110009_N0513_R094_T30UXB_... (Earth Search's s2:product_uri, CDSE's Name)."""
    m = re.match(r'(S2[A-D])_MSIL(?:1C|2A)_(\d{8}T\d{6})_N\d+_R(\d+)_T(\w{5})_', name or '')
    return (m[1], m[2], int(m[3]), m[4]) if m else None


def cdse_not_yet_ingested(lat, lon, start, end, have):
    """L1C products on Copernicus Data Space (listed ~20-90 min before Earth Search has the L2A)
    whose datatake/tile isn't in `have` (product_key values). Discovery only: no pixels or tile
    time until Earth Search. `start` is an ISO time; pass the newest Earth Search scene less a day."""
    flt = (f"Collection/Name eq 'SENTINEL-2' and contains(Name,'MSIL1C') and "
           f"OData.CSC.Intersects(area=geography'SRID=4326;POINT({lon} {lat})') and "
           f"ContentDate/Start gt {start[:19]}.000Z and ContentDate/Start lt {end}T23:59:59.000Z")
    r = requests.get(CDSE, params={'$filter': flt, '$top': 50, '$orderby': 'ContentDate/Start desc',
                                   '$expand': 'Attributes'}, timeout=60)
    r.raise_for_status()
    out = {}
    for v in r.json().get('value', []):
        if (key := product_key(v['Name'])) and key not in have:
            a = {x['Name']: x['Value'] for x in v.get('Attributes', [])}
            out[key] = dict(sat=key[0], tile=key[3], orbit=key[2], datatake=v['ContentDate']['Start'][:19] + 'Z',
                            cloud=a.get('cloudCover'))
    return sorted(out.values(), key=lambda p: p['datatake'])


# ---------------------------------------------------------------- ADS-B
class Track:
    """Time-sorted ADS-B positions in a scene's UTM; heights above the surface."""

    def __init__(self, rows, scene, label=''):
        rows = sorted((r for r in rows if r['lat'] is not None and r['lon'] is not None), key=lambda r: r['t'])
        if len(rows) < 2:
            raise ValueError('need at least 2 positions')
        self.label = label
        self.t = np.array([r['t'] for r in rows])
        xy = np.array([scene.utm(r['lat'], r['lon']) for r in rows]); self.x, self.y = xy[:, 0], xy[:, 1]
        self.geo = np.array([np.nan if r.get('geo') in (None, '') else float(r['geo']) for r in rows])
        self.baro = np.array([np.nan if r.get('baro') in (None, '') else float(r['baro']) for r in rows])
        self.gs = np.array([np.nan if r.get('gs') in (None, '') else float(r['gs']) for r in rows])
        self.scene = scene
        self.ground = np.array([int(r.get('ground') or 0) for r in rows])
        self._surface = None

    def at(self, t):
        return float(np.interp(t, self.t, self.x)), float(np.interp(t, self.t, self.y))

    def surface(self):
        """(geoid, terrain) at the aircraft's position near the imaging time."""
        if self._surface is None:
            lat, lon = self.scene.latlon(*self.at(self.scene.model_time(np.interp(self.scene.sensing, self.t, self.y))))
            self._surface = (geoid_m(lat, lon), dem_m(lat, lon))
        return self._surface

    def height(self, t):
        """Height above the terrain the image is orthorectified to (m), and its basis."""
        N, dem = self.surface()
        g = ~np.isnan(self.geo)
        if g.sum() >= 2:
            return float(np.interp(t, self.t[g], self.geo[g])) * 0.3048 - N - dem, 'geometric alt - geoid - terrain'
        b = ~np.isnan(self.baro)
        if b.sum() >= 1:
            return float(np.interp(t, self.t[b], self.baro[b])) * 0.3048 - dem, 'baro alt - terrain (baro is approximate)'
        if self.ground[np.argmin(np.abs(self.t - t))]:
            return 0.0, 'on the ground (ADS-B on_ground)'
        return float('nan'), 'no altitude'

    def gap(self, t, half=10):
        m = (self.t > t - half - 60) & (self.t < t + half + 60); tt = np.sort(self.t[m])
        if len(tt) < 2 or tt[0] > t - half or tt[-1] < t + half:
            return float('inf')
        return float(np.diff(tt).max())

    def speed_kt(self, t):
        g = ~np.isnan(self.gs)
        return float(np.interp(t, self.t[g], self.gs[g])) if g.any() else float('nan')


def hexbird_sql(sql):
    base = os.environ.get('HEXBIRD_API_BASE', 'https://hexbird.io').rstrip('/')
    tok = os.environ.get('HEXBIRD_TOKEN')
    if not tok:
        sys.exit('HEXBIRD_TOKEN is not set (needed for --hex / identify); or pass --track CSV')
    r = requests.post(f'{base}/api/v1/tools/query_adsb_tracks', headers={'Authorization': f'Bearer {tok}'},
                      json={'sql': sql, 'format': 'JSONEachRow'}, timeout=300)
    if not r.ok:
        sys.exit(f'Hexbird query failed: {r.status_code} {r.text[:300]}')
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def adsb_rows(where, t0, t1):
    """Positions from default.traces and default.recent (live 48 h), merged."""
    a, b = [datetime.fromtimestamp(v, timezone.utc).strftime('%Y-%m-%d %H:%M:%S') for v in (t0, t1)]
    cols = "icao24, any(registration) reg, any(aircraft_type) type, any(callsign) cs, groupArray((toUnixTimestamp64Milli(timestamp)/1000, latitude, longitude, geo_altitude, baro_altitude, ground_speed, on_ground)) pts"
    out = {}
    for table in ['traces', 'recent']:
        sql = f"SELECT {cols} FROM (SELECT * FROM default.{table} WHERE {where} AND timestamp BETWEEN '{a}' AND '{b}' ORDER BY icao24, timestamp) GROUP BY icao24"
        for r in hexbird_sql(sql):
            d = out.setdefault(r['icao24'], {'reg': r['reg'], 'type': r['type'], 'cs': (r['cs'] or '').strip(), 'pts': {}})
            for p in r['pts']:
                d['pts'][round(p[0], 1)] = {'t': p[0], 'lat': p[1], 'lon': p[2], 'geo': p[3], 'baro': p[4], 'gs': p[5], 'ground': p[6]}
    return {k: dict(v, rows=list(v['pts'].values())) for k, v in out.items()}


def read_track_csv(path):
    rows = []
    for r in csv.DictReader(open(path)):
        rows.append({'t': parse_time(r['time']), 'lat': float(r['lat']), 'lon': float(r['lon']),
                     'geo': r.get('alt_geom_ft') or None, 'baro': r.get('alt_baro_ft') or None, 'gs': r.get('gs_kt') or None})
    return rows


# ---------------------------------------------------------------- aircraft search
def lag(band, det):
    return LAG[band] if det % 2 else LAG_EVEN[band]


def image_pos(scene, tr, band, t, h):
    """Where `band` shows the aircraft; t = start of that line's band sequence."""
    v0 = scene.view(band, *tr.at(t))
    if v0 is None:
        return None
    x, y = tr.at(t + lag(band, v0[2]))
    v = scene.view(band, x, y)
    if v is None:
        return None
    zen, az, _ = v
    d = h * math.tan(math.radians(zen)); a = math.radians(az + 180)
    return x + d * math.sin(a), y + d * math.cos(a)


def anomaly(a, size=11):
    an = a - median_filter(a, size=size)
    noise = 1.4826 * np.median(np.abs(an - np.median(an)))
    return an / max(noise, 1e-6)


def find(scene, tr, half=SEARCH_HALF, step=0.05):
    """Matched-filter search for one aircraft; returns a result dict (detected or not)."""
    y_mid = float(np.interp(scene.sensing, tr.t, tr.y))
    tm = scene.model_time(y_mid)
    for _ in range(2):  # refine the model time with the aircraft's own position
        tm = scene.model_time(tr.at(tm)[1])
    h, hbasis = tr.height(tm)
    res = {'aircraft': tr.label, 'item': scene.item_id, 'model_time': iso(tm), 'height_m': h, 'height_basis': hbasis,
           'surface_geoid_m': tr.surface()[0], 'surface_terrain_m': tr.surface()[1], 'detected': False, 'flags': []}
    if not math.isfinite(h):
        res['flags'].append('no altitude'); return res
    if h < 30 and 'ground' in hbasis:
        res['flags'].append('on the ground: no parallax and almost no motion between bands, so no colour fringe to find'); return res
    if tr.t.min() > tm - half or tr.t.max() < tm + half:
        res['flags'].append(f'ADS-B track does not cover {iso(tm - half, False)}..{iso(tm + half, False)}')
        if tr.t.min() > tm + 5 or tr.t.max() < tm - 5:
            return res
    times = np.arange(tm - half, tm + half, step)
    pos = {b: [image_pos(scene, tr, b, t, h) for t in times] for b in BANDS}
    ok = np.array([all(pos[b][i] is not None for b in BANDS) for i in range(len(times))])
    if ok.sum() < 10:
        res['flags'].append('aircraft not inside the scene\'s detector footprint near the imaging time'); return res
    times = times[ok]; pos = {b: [p for p, k in zip(pos[b], ok) if k] for b in BANDS}
    allxy = np.array([p for b in BANDS for p in pos[b]])
    x0, y0 = allxy.min(0) - 500; x1, y1 = allxy.max(0) + 500
    z, origin = {}, None
    for b in BANDS:
        a, ox, oy = scene.read(b, x0, y0, x1, y1)
        if a.size == 0 or (a == 0).mean() > 0.3:
            res['flags'].append('no image data along the path (outside tile or nodata)'); return res
        z[b] = anomaly(a); origin = (ox, oy)
    ox, oy = origin
    rc = lambda p: ((oy - p[1]) / 10 - 0.5, (p[0] - ox) / 10 - 0.5)
    zt = {b: maximum_filter(np.abs(z[b]), size=TOL_PX) for b in BANDS}
    score = np.zeros(len(times)); per = {}
    for b in BANDS:
        rr, cc = np.array([rc(p) for p in pos[b]]).T
        per[b] = map_coordinates(zt[b], [rr, cc], order=0, mode='nearest')
        score += np.minimum(per[b], ZCAP)
    i = int(score.argmax())
    far = np.abs(times - times[i]) > 1.0
    bg = score[far] if far.sum() > 10 else score
    sig = float((score[i] - np.median(bg)) / (1.4826 * np.median(np.abs(bg - np.median(bg))) + 1e-9))
    tb = float(times[i])
    # measure each band's centroid near its prediction; window scales with the fringe
    pred = {b: pos[b][i] for b in BANDS}
    spread = max(math.hypot(pred[a][0] - pred[b][0], pred[a][1] - pred[b][1]) for a in BANDS for b in BANDS) / 10
    win = int(3 + 0.25 * spread)
    meas, peak = {}, {}
    for b in BANDS:
        r0, c0 = rc(pred[b]); R, C = int(round(r0)), int(round(c0))
        sub = np.abs(z[b][max(R - win, 0):R + win + 1, max(C - win, 0):C + win + 1])
        if sub.size == 0:
            continue
        rr, cc = np.unravel_index(sub.argmax(), sub.shape); R2, C2 = max(R - win, 0) + rr, max(C - win, 0) + cc
        blk = np.abs(z[b][max(R2 - 1, 0):R2 + 2, max(C2 - 1, 0):C2 + 2]); gy, gx = np.mgrid[max(R2 - 1, 0):R2 + 2, max(C2 - 1, 0):C2 + 2][:, :blk.shape[0], :blk.shape[1]]
        meas[b] = (ox + ((gx * blk).sum() / blk.sum() + 0.5) * 10, oy - ((gy * blk).sum() / blk.sum() + 0.5) * 10)
        peak[b] = float(sub.max())
    nb = int(sum(peak.get(b, 0) >= DETECT_Z for b in BANDS))
    # refine the time against the measured centroids of the bands that see it
    seen = [b for b in BANDS if peak.get(b, 0) >= DETECT_Z]
    if len(seen) >= 2:
        fine = np.arange(tb - 1.5, tb + 1.5, 0.01)
        cost = []
        for t in fine:
            c = 0.0
            for b in seen:
                p = image_pos(scene, tr, b, t, h)
                c += (p[0] - meas[b][0]) ** 2 + (p[1] - meas[b][1]) ** 2 if p else 1e9
            cost.append(c)
        tb2 = float(fine[int(np.argmin(cost))]); pred2 = {b: image_pos(scene, tr, b, tb2, h) for b in BANDS}
        if all(pred2[b] is not None for b in BANDS):
            tb, pred = tb2, pred2
        rms = math.sqrt(min(cost) / len(seen))
    else:
        rms = float('nan')
    cx, cy = np.mean([pred[b] for b in BANDS], axis=0)
    lat, lon = scene.latlon(cx, cy)
    v = scene.view('B03', cx, cy) or scene.view('B03', *tr.at(tb))
    if v is None:
        res['flags'].append('match is outside the viewing-angle grid (tile edge)'); return res
    det = v[2]
    scl = scene.scl_at(cx, cy)
    first, last = ('B02', 'B04') if det % 2 else ('B04', 'B02')
    fr_p = np.subtract(pred[last], pred[first])
    fr_m = np.subtract(meas[last], meas[first]) if first in meas and last in meas else None
    res.update({
        'time': iso(tb), 'dt_vs_model_s': tb - tm, 'dt_vs_sensing_s': tb - scene.sensing, 'significance': sig, 'bands_seen': nb,
        'band_peak_z': peak, 'fit_rms_m': rms, 'image_lat': lat, 'image_lon': lon, 'detector': det, 'scl': SCL_NAMES.get(scl, str(scl)),
        'gs_kt': tr.speed_kt(tb), 'adsb_max_gap_s': tr.gap(tb),
        'pred': {b: list(map(float, pred[b])) for b in BANDS}, 'meas': {b: list(map(float, v)) for b, v in meas.items()},
        'fringe_pred_m': float(np.hypot(*fr_p)), 'fringe_pred_deg': float(math.degrees(math.atan2(*fr_p)) % 360),
    })
    if fr_m is not None:
        res['fringe_meas_m'] = float(np.hypot(*fr_m)); res['fringe_meas_deg'] = float(math.degrees(math.atan2(*fr_m)) % 360)
        res['fringe_dir_agrees'] = bool(np.dot(fr_m, fr_p) > 0)
    # verdict
    if nb < 3 or sig < 6:
        res['flags'].append(f'weak: {nb} of 4 bands, significance {sig:.1f}')
    if abs(tb - tm) > 3 * T_RMS:
        res['flags'].append(f'imaging time {tb - tm:+.1f} s from the timing model (>|{3 * T_RMS:.0f}| s): likely a false match')
    if scl in (8, 9, 10):
        res['flags'].append(f'match sits on {SCL_NAMES[scl]}: likely a false match')
    if h < 600 and scl != 6:
        res['flags'].append('low aircraft over land: ground features can mimic it; check by eye')
    if fr_m is not None and not res['fringe_dir_agrees']:
        res['flags'].append('measured fringe runs opposite to the prediction')
    if math.isfinite(rms) and rms > 40:
        res['flags'].append(f'band positions fit poorly ({rms:.0f} m rms)')
    res['detected'] = nb >= 3 and sig >= 6 and not any('likely a false' in f or 'opposite' in f for f in res['flags'])
    res['_draw'] = dict(z=z, origin=origin, tr=tr, h=h, tb=tb)
    return res


def overlay(scene, res, path):
    """PNG: RGB crop around the match with the apparent ADS-B path, predicted band
    positions and fringe axis, plus a wider context panel."""
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    d = res['_draw']; tr, h, tb = d['tr'], d['h'], d['tb']
    c = np.mean([res['pred'][b] for b in BANDS], axis=0)
    half = max(200, int(res['fringe_pred_m'] * 0.8 + 120))
    fig, axs = plt.subplots(1, 2, figsize=(12, 6.3), dpi=120)
    for ax, hw in zip(axs, [half, max(1500, half * 6)]):
        a = {b: scene.read(b, c[0] - hw, c[1] - hw, c[0] + hw, c[1] + hw) for b in ['B02', 'B03', 'B04']}
        n0 = min(v[0].shape[0] for v in a.values()); n1 = min(v[0].shape[1] for v in a.values())
        rgb = np.dstack([a['B04'][0][:n0, :n1], a['B03'][0][:n0, :n1], a['B02'][0][:n0, :n1]])
        lo, hi = np.percentile(rgb, 1), np.percentile(rgb, 99.9 if hw == half else 99.5)
        ox, oy = a['B02'][1], a['B02'][2]
        ax.imshow(np.clip((rgb - lo) / (hi - lo), 0, 1) ** 0.9, extent=(ox - c[0], ox + n1 * 10 - c[0], oy - n0 * 10 - c[1], oy - c[1]), interpolation='nearest')
        ts = np.arange(tb - 60, tb + 60, 0.25)
        P = np.array([p for p in (image_pos(scene, tr, 'B03', t, h) for t in ts) if p is not None]) - c
        ax.plot(P[:, 0], P[:, 1], '-', color='#ffd400', lw=1.2, alpha=0.8, label='ADS-B path as the satellite sees it')
        # direction arrow just past the match, inside this panel
        inside = np.where((np.abs(P[:, 0]) < hw * 0.75) & (np.abs(P[:, 1]) < hw * 0.75))[0]
        if len(inside) > 2:
            j = inside[-1]; k = max(inside[0], j - 3)
            ax.annotate('', xy=P[j], xytext=P[k], arrowprops=dict(arrowstyle='-|>', color='#ffd400', lw=1.3, mutation_scale=14))
        F = np.array([res['pred']['B02'], res['pred']['B04']]) - c
        ax.plot(F[:, 0], F[:, 1], '--', color='white', lw=1, dashes=(3, 2), label='predicted fringe axis')
        for b, col in [('B02', '#7fb2ff'), ('B03', '#7dff7d'), ('B04', '#ff7d7d')]:
            ax.plot(*(np.array(res['pred'][b]) - c), 'x', ms=7, color=col, mew=1.4)
        ax.set_xlim(-hw, hw); ax.set_ylim(-hw, hw); ax.set_xlabel('m (grid east)'); ax.set_ylabel('m (grid north)')
        if hw == half:
            ax.legend(loc='lower left', fontsize=8, framealpha=0.8)
    v = 'DETECTED' if res['detected'] else 'not confirmed'
    axs[0].set_title(f"{res['aircraft']}  {v}\n{res['time']}  {res['height_m'] / 0.3048:,.0f} ft above surface", fontsize=10)
    axs[1].set_title(f"{scene.item_id}\ncontext", fontsize=10)
    fig.text(0.01, 0.01, 'x = predicted blue/green/red positions at the fitted time. Sentinel-2 L2A (Copernicus, Element84 Earth Search). ' + '; '.join(res['flags'])[:180], fontsize=7.5, color='#333')
    plt.tight_layout(rect=(0, 0.03, 1, 1)); plt.savefig(path); plt.close(fig)


def report(res):
    keys = ['aircraft', 'detected', 'time', 'model_time', 'dt_vs_model_s', 'significance', 'bands_seen', 'fit_rms_m', 'image_lat', 'image_lon',
            'height_m', 'height_basis', 'gs_kt', 'fringe_pred_m', 'fringe_pred_deg', 'fringe_meas_m', 'fringe_meas_deg', 'detector', 'scl', 'adsb_max_gap_s', 'flags']
    for k in keys:
        if k in res:
            v = res[k]
            print(f'  {k:18s} {v:.5f}' if isinstance(v, float) and k in ('image_lat', 'image_lon') else f'  {k:18s} {v:.2f}' if isinstance(v, float) else f'  {k:18s} {v}')


def save(res, out, name, scene):
    os.makedirs(out, exist_ok=True)
    png = os.path.join(out, f'{name}.png')
    if '_draw' in res:
        overlay(scene, res, png); res['overlay_png'] = png
    clean = {k: v for k, v in res.items() if not k.startswith('_')}
    json.dump(clean, open(os.path.join(out, f'{name}.json'), 'w'), indent=1, default=float)
    return clean


# ---------------------------------------------------------------- crops (for looking)
def crop(lat, lon, size_m, items, grid, nir, out):
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    n = len(items); fig, axs = plt.subplots(1, n, figsize=(7 * n, 7.3), dpi=130, squeeze=False)
    first = None
    for ax, item in zip(axs[0], items):
        sc = Scene(item); x, y = sc.utm(lat, lon); hw = size_m / 2
        bands = ['B08', 'B04', 'B03'] if nir else ['B04', 'B03', 'B02']
        # big crops come from the overviews (fast); a ship or aircraft is
        # invisible below ~10 m/pixel anyway, so use them to find wakes and context
        a = [sc.read(b, x - hw, y - hw, x + hw, y + hw, max_px=CROP_MAX_PX) for b in bands]
        n0 = min(v[0].shape[0] for v in a); n1 = min(v[0].shape[1] for v in a)
        rgb = np.dstack([v[0][:n0, :n1] for v in a])
        px = max(10.0, size_m / max(n0, n1))
        lo, hi = np.percentile(rgb, 1), np.percentile(rgb, 99.5)
        ax.imshow(np.clip((rgb - lo) / (hi - lo), 0, 1) ** 0.85, extent=(-hw, -hw + n1 * px, hw - n0 * px, hw), interpolation='nearest' if size_m <= 3000 else 'antialiased')
        ax.plot(0, 0, '+', color='red', ms=14, mew=1.5)
        if grid:
            step = size_m / grid
            for k in range(1, grid):
                ax.axvline(-hw + k * step, color='white', lw=0.5, alpha=0.5); ax.axhline(-hw + k * step, color='white', lw=0.5, alpha=0.5)
            for r in range(grid):
                for cidx in range(grid):
                    ax.text(-hw + cidx * step + step * 0.04, hw - r * step - step * 0.04, f'{chr(65 + r)}{cidx + 1}', color='white', fontsize=8, va='top', alpha=0.8)
        ax.set_xlim(-hw, hw); ax.set_ylim(-hw, hw)
        ax.set_title(f"{item}\nSENSING_TIME {iso(sc.sensing)}  ({'NIR false colour' if nir else 'true colour'}, {px:.0f} m/pixel)", fontsize=9)
        ax.set_xlabel('m east of centre (grid)'); ax.set_ylabel('m north of centre (grid)')
        first = first or sc
    fig.text(0.01, 0.01, f'centre (red +) {lat:.5f}, {lon:.5f}; {size_m / 1000:.1f} km across. Grid cells: row letter from the top, column number from the left. Sentinel-2 L2A, Copernicus.', fontsize=8)
    plt.tight_layout(rect=(0, 0.03, 1, 1)); plt.savefig(out); plt.close(fig)
    print(f'wrote {out}')


def blob(sc, x, y, radius=300):
    nir, ox, oy = sc.read('B08', x - radius, y - radius, x + radius, y + radius)
    r = nir - median_filter(nir, size=25); noise = 1.4826 * np.median(np.abs(r - np.median(r)))
    lab, n = label(r > 5 * noise)
    if n == 0:
        return None
    best, bd = None, 1e18
    for i in range(1, n + 1):
        rr, cc = np.nonzero(lab == i)
        if len(rr) < 2:
            continue
        X = ox + (cc + 0.5) * 10; Y = oy - (rr + 0.5) * 10; w = r[rr, cc]
        cx, cy = (X * w).sum() / w.sum(), (Y * w).sum() / w.sum()
        dd = math.hypot(cx - x, cy - y) - 0.5 * math.sqrt(len(rr)) * 10
        if dd < bd:
            bd, best = dd, (cx, cy, X, Y, w)
    if best is None:
        return None
    cx, cy, X, Y, w = best
    cov = np.cov(np.vstack([X, Y]), aweights=w) if len(X) > 2 else np.eye(2)
    ev, evec = np.linalg.eigh(cov); ax = evec[:, -1]
    proj = (X - cx) * ax[0] + (Y - cy) * ax[1]
    pk = int(np.argmax(w))
    return dict(x=cx, y=cy, n=len(X), length_m=float(proj.max() - proj.min() + 10), axis_deg=float(math.degrees(math.atan2(ax[0], ax[1])) % 180),
                bright_end_deg=float(math.degrees(math.atan2(X[pk] - cx, Y[pk] - cy)) % 360))


def ships(item, lat, lon, size_m, compare, top, out):
    """Candidate ships in a box: compact bright NIR objects on water, away from
    cloud fields, optionally not present at the same spot in a second scene
    (fixed objects). Writes a contact sheet of chips for you to judge by eye."""
    import time
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    from scipy.ndimage import binary_dilation, find_objects
    t0 = time.time()
    if size_m > SHIPS_MAX_M:
        print(f'box clamped to {SHIPS_MAX_M / 1000:.0f} km (bigger scans are slow and swamp you with candidates; pick the area from ADS-B clues)')
        size_m = SHIPS_MAX_M
    sc = Scene(item); x, y = sc.utm(lat, lon); hw = size_m / 2

    def bright(scene):
        nir, ox, oy = scene.read('B08', x - hw, y - hw, x + hw, y + hw)
        bg = np.kron(median_filter(nir[::5, ::5], size=9), np.ones((5, 5)))[:nir.shape[0], :nir.shape[1]]
        r = nir - bg
        return r, 1.4826 * np.median(np.abs(r - np.median(r))), ox, oy

    r, noise, ox, oy = bright(sc)
    mask = r > 8 * noise
    scl, sox, soy = sc.read('SCL', x - hw, y - hw, x + hw, y + hw)
    fixed_mask = None
    if compare:
        r2, n2, ox2, oy2 = bright(Scene(compare))
        if r2.shape == mask.shape and abs(ox2 - ox) < 1 and abs(oy2 - oy) < 1:
            fixed_mask = binary_dilation(r2 > 8 * n2, iterations=3)
        else:
            print(f'{compare} is on a different grid; fixed-object removal skipped')
    lab, n = label(mask)
    cands = []
    for i, sl in enumerate(find_objects(lab), 1):
        sub = lab[sl] == i; npx = int(sub.sum())
        if npx < 3 or npx > 800:
            continue
        # cloud fields: many bright pixels around the object
        R0, R1 = max(sl[0].start - 40, 0), sl[0].stop + 40; C0, C1 = max(sl[1].start - 40, 0), sl[1].stop + 40
        if mask[R0:R1, C0:C1].mean() > 0.03:
            continue
        rr, cc = np.nonzero(sub); rr = rr + sl[0].start; cc = cc + sl[1].start
        w = r[rr, cc]; X = ox + (cc + 0.5) * 10; Y = oy - (rr + 0.5) * 10
        cx, cy = (X * w).sum() / w.sum(), (Y * w).sum() / w.sum()
        si, sj = int((soy - cy) / 20), int((cx - sox) / 20)
        cls = int(scl[si, sj]) if 0 <= si < scl.shape[0] and 0 <= sj < scl.shape[1] else 0
        if cls in (8, 9, 10):
            continue
        if fixed_mask is not None and fixed_mask[rr, cc].any():
            continue  # bright here in the other scene too: turbine, platform, buoy, moored ship
        cov = np.cov(np.vstack([X, Y]), aweights=w) if npx > 2 else np.eye(2)
        ev, evec = np.linalg.eigh(cov); ax_ = evec[:, -1]
        proj = (X - cx) * ax_[0] + (Y - cy) * ax_[1]
        la, lo = sc.latlon(cx, cy)
        cands.append(dict(lat=la, lon=lo, x=cx, y=cy, px=npx, snr=float(w.max() / noise), length_m=float(proj.max() - proj.min() + 10),
                          axis_deg=float(math.degrees(math.atan2(ax_[0], ax_[1])) % 180), scl=SCL_NAMES.get(cls, str(cls))))
    cands.sort(key=lambda c: -c['snr'] * math.sqrt(c['px']))
    shown = cands[:top]
    print(f'{item}: {len(cands)} candidates in {size_m / 1000:.0f} km box' + (f' (fixed objects in {compare} removed)' if fixed_mask is not None else '') + f'; showing {len(shown)}; {time.time() - t0:.0f} s')
    for k, c in enumerate(shown):
        print(f"  #{k:2d} {c['lat']:.5f},{c['lon']:.5f}  {c['px']:3d} px  ~{c['length_m']:.0f} m along {c['axis_deg']:.0f}/{c['axis_deg'] + 180:.0f} deg  snr {c['snr']:.0f}  {c['scl']}")
    if shown:
        cols = 6; rows = math.ceil(len(shown) / cols); h = 50  # 1 km chips
        fig, axs = plt.subplots(rows, cols, figsize=(cols * 2.6, rows * 2.85), dpi=100, squeeze=False)
        for axx in axs.flat:
            axx.axis('off')
        for axx, (k, c) in zip(axs.flat, enumerate(shown)):
            a = [sc.read(b, c['x'] - h * 10, c['y'] - h * 10, c['x'] + h * 10, c['y'] + h * 10)[0] for b in ['B04', 'B03', 'B02']]
            n0 = min(v.shape[0] for v in a); n1 = min(v.shape[1] for v in a)
            rgb = np.dstack([v[:n0, :n1] for v in a]); lo_, hi_ = np.percentile(rgb, 1), np.percentile(rgb, 99.9)
            axx.imshow(np.clip((rgb - lo_) / (hi_ - lo_), 0, 1) ** 0.8, interpolation='nearest')
            axx.set_title(f"#{k} {c['lat']:.4f},{c['lon']:.4f}\n~{c['length_m']:.0f} m, snr {c['snr']:.0f}", fontsize=8)
        fig.suptitle(f'{item} {iso(sc.sensing)}: ship candidates (1 km chips, north up). Judge each by eye: hull + wake = ship; fluffy/with shadow = cloud.', fontsize=9)
        plt.tight_layout(); plt.savefig(out); plt.close(fig)
        print(f'wrote {out}')


def ship_motion(s1, s2):
    out = []
    for item, lat, lon in (s1, s2):
        sc = Scene(item); x, y = sc.utm(lat, lon); b = blob(sc, x, y)
        if b is None:
            sys.exit(f'no bright object within 300 m of {lat},{lon} in {item}')
        la, lo = sc.latlon(b['x'], b['y'])
        print(f"{item}  {iso(sc.sensing)}  object at {la:.5f},{lo:.5f}: {b['n']} px, extent along axis ~{b['length_m']:.0f} m (includes wake), "
              f"axis {b['axis_deg']:.0f}/{b['axis_deg'] + 180:.0f} deg grid, brightest pixel toward {b['bright_end_deg']:.0f} deg")
        out.append((sc.sensing, la, lo))
    (t1, la1, lo1), (t2, la2, lo2) = out
    from pyproj import Geod
    az, _, dist = Geod(ellps='WGS84').inv(lo1, la1, lo2, la2)
    print(f'moved {dist:.0f} m in {t2 - t1:.1f} s: {dist / (t2 - t1) * 1.94384:.1f} kt on course {az % 360:.0f} deg true')
    print('(ships are at the surface: no parallax correction; band offsets inside one image are tiny for ships)')


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest='cmd', required=True)
    a = sp.add_parser('scenes'); a.add_argument('lat', type=float); a.add_argument('lon', type=float); a.add_argument('start'); a.add_argument('end'); a.add_argument('--max-cloud', type=float, default=60)
    a = sp.add_parser('when'); a.add_argument('item'); a.add_argument('lat', type=float); a.add_argument('lon', type=float)
    a = sp.add_parser('find'); a.add_argument('item'); g = a.add_mutually_exclusive_group(required=True); g.add_argument('--hex'); g.add_argument('--track'); a.add_argument('--out', default='s2out')
    a = sp.add_parser('identify'); a.add_argument('item'); a.add_argument('lat', type=float); a.add_argument('lon', type=float); a.add_argument('--radius-km', type=float, default=15); a.add_argument('--out', default='s2out')
    a = sp.add_parser('crop'); a.add_argument('lat', type=float); a.add_argument('lon', type=float); a.add_argument('size_m', type=float); a.add_argument('items', nargs='+'); a.add_argument('--grid', type=int, default=0); a.add_argument('--nir', action='store_true'); a.add_argument('--out', default='crop.png')
    a = sp.add_parser('ships'); a.add_argument('item'); a.add_argument('lat', type=float); a.add_argument('lon', type=float); a.add_argument('size_m', type=float); a.add_argument('--compare'); a.add_argument('--top', type=int, default=24); a.add_argument('--out', default='ships.png')
    a = sp.add_parser('ship-motion'); a.add_argument('item1'); a.add_argument('lat1', type=float); a.add_argument('lon1', type=float); a.add_argument('item2'); a.add_argument('lat2', type=float); a.add_argument('lon2', type=float)
    o = p.parse_args()

    if o.cmd == 'scenes':
        feats = search_scenes(o.lat, o.lon, o.start, o.end)
        shown = [f for f in feats if f['properties'].get('eo:cloud_cover', 0) < o.max_cloud]
        def line(f):
            sc = Scene(f['id'], f); x, y = sc.utm(o.lat, o.lon); c = sc.scl_at(x, y)
            return (f"{f['id']:34s} SENSING_TIME {iso(sc.sensing)}  point imaged ~{iso(sc.model_time(y))} (+-{T_RMS:.0f} s)  "
                    f"cloud {f['properties'].get('eo:cloud_cover', 0):4.0f}%  "
                    f"{SCL_NAMES.get(c) + ' at point' if c else 'no data at point (outside the swath)'}")
        with ThreadPoolExecutor(8) as pool:  # each scene is 3 small reads: metadata, angles, SCL
            for text in pool.map(line, shown):
                print(text)
        if len(shown) < len(feats):
            print(f'{len(feats) - len(shown)} more scene(s) with tile cloud >= {o.max_cloud:.0f}% hidden (tile-wide cloud can still '
                  f'leave the point clear: rerun with --max-cloud 101 to list them)')
        try:
            since = f"{o.start}T00:00:00"
            if feats:  # anything older than the newest Earth Search scene is ingested already
                since = max(since, iso(parse_time(feats[-1]['properties']['datetime']) - 86400))
            extra = cdse_not_yet_ingested(o.lat, o.lon, since, o.end,
                                          {product_key(f['properties'].get('s2:product_uri')) for f in feats})
        except (requests.RequestException, ValueError):
            extra = []
        if extra:
            print('Also on Copernicus Data Space, not yet on Earth Search (s2tool can read them once Earth Search has the '
                  'L2A, usually within ~1-2 h. The point is imaged later than the datatake start, often by several minutes):')
            for p in extra:
                cl = f"cloud {p['cloud']:.0f}%" if p['cloud'] is not None else ''
                print(f"  {p['sat']} tile {p['tile']} orbit R{p['orbit']:03d}  datatake started {p['datatake']}  {cl}")
    elif o.cmd == 'when':
        sc = Scene(o.item); x, y = sc.utm(o.lat, o.lon); v = sc.view('B03', x, y)
        print(f'SENSING_TIME (tile) {iso(sc.sensing)}; this point imaged ~{iso(sc.model_time(y))} +-{T_RMS:.0f} s rms ({sc.km_north_of_centre(y):+.1f} km north of tile centre)')
        if v:
            print(f'view zenith {v[0]:.2f} deg, satellite azimuth {v[1]:.1f} deg, detector {v[2]} ({"odd: B02 first" if v[2] % 2 else "even: B04 first"})')
        print(f'scene class at point: {SCL_NAMES.get(sc.scl_at(x, y))}')
    elif o.cmd == 'find':
        sc = Scene(o.item)
        if o.track:
            rows, label_ = read_track_csv(o.track), os.path.basename(o.track)
        else:
            hx = o.hex.lower()
            d = adsb_rows(f"icao24 = '{hx}'", sc.sensing - 180, sc.sensing + 180).get(hx)
            if not d:
                sys.exit(f'no ADS-B for {hx} within 3 min of {iso(sc.sensing)}')
            rows, label_ = d['rows'], ' '.join(x for x in [hx, d['type'], d['reg'], d['cs']] if x)
        res = find(sc, Track(rows, sc, label_))
        clean = save(res, o.out, f"{o.item[:24]}_{(o.hex or 'track').lower()}", sc)
        report(clean)
        if 'overlay_png' in clean:
            print(f"  overlay            {clean['overlay_png']}")
    elif o.cmd == 'identify':
        sc = Scene(o.item); x, y = sc.utm(o.lat, o.lon); tm = sc.model_time(y)
        dl = o.radius_km / 111.32 + 0.3; dlo = dl / math.cos(math.radians(o.lat))
        where = f'latitude BETWEEN {o.lat - dl} AND {o.lat + dl} AND longitude BETWEEN {o.lon - dlo} AND {o.lon + dlo}'
        cands = adsb_rows(where, tm - 120, tm + 120)
        print(f'{len(cands)} aircraft with ADS-B within ~{o.radius_km + 33:.0f} km around {iso(tm, False)}; testing those that pass near the point')
        results = []
        for hx, d in cands.items():
            try:
                tr = Track(d['rows'], sc, ' '.join(v for v in [hx, d['type'], d['reg'], d['cs']] if v))
            except ValueError:
                continue
            px, py = tr.at(tm)
            if math.hypot(px - x, py - y) > o.radius_km * 1000 + 3000:
                continue
            if tr.ground[np.argmin(np.abs(tr.t - tm))]:
                continue  # on the ground at the imaging time
            try:
                r = find(sc, tr)
            except Exception as e:
                print(f'  {hx}: error {type(e).__name__}: {e}'); continue
            if 'pred' in r:
                r['miss_m'] = math.hypot(np.mean([r['pred'][b][0] for b in BANDS]) - x, np.mean([r['pred'][b][1] for b in BANDS]) - y)
            results.append((r, hx))
        results.sort(key=lambda rr: (not rr[0]['detected'], rr[0].get('miss_m', 1e9)))
        for r, hx in results:
            print(f"{'DETECTED ' if r['detected'] else '         '}{r['aircraft'][:40]:40s} image pos {r.get('miss_m', float('nan')):7.0f} m from point  "
                  f"time {r.get('time', '-')}  sig {r.get('significance', float('nan')):5.1f}  {'; '.join(r['flags'])[:90]}")
        for r, hx in results[:3]:
            if r['detected']:
                save(r, o.out, f'{o.item[:24]}_{hx}', sc); print(f'  overlay {o.out}/{o.item[:24]}_{hx}.png')
    elif o.cmd == 'crop':
        crop(o.lat, o.lon, o.size_m, o.items, o.grid, o.nir, o.out)
    elif o.cmd == 'ships':
        ships(o.item, o.lat, o.lon, o.size_m, o.compare, o.top, o.out)
    elif o.cmd == 'ship-motion':
        ship_motion((o.item1, o.lat1, o.lon1), (o.item2, o.lat2, o.lon2))


if __name__ == '__main__':
    main()
