#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pyproj", "rasterio", "requests", "matplotlib"]
# ///
"""Sentinel-1 (C-band radar) tools for ship work: find passes over a place and the moment it was
imaged, detect ships (day or night, through cloud), flag fixed objects, predict where a moving ship
appears in the image, and render crops.

  s1tool.py scenes LAT LON START END
  s1tool.py when ITEM LAT LON [--sog KT --cog DEG [--at TIME]]
  s1tool.py ships ITEM LAT LON SIZE_M [--compare 3] [--min-db -6] [--top 24] [--out DIR]
  s1tool.py crop LAT LON SIZE_M ITEM [ITEM ...] [--pol vv|vh] [--out PNG]

ITEM is a Sentinel-1 IW GRD id such as S1D_IW_GRDH_1SDV_20261012T170109_20261012T170134_004985_009693
(from `scenes`). Imagery comes from Microsoft Planetary Computer: the geocoded, calibrated RTC product
when it exists (usually 12-24 h after the pass), else the GRD product (~4 h after the pass), which
this tool geocodes from the scene's tie points and calibrates itself. Land is masked with the
Copernicus GLO-30 DEM. `ships` marks mapped structures from OpenStreetMap through the Hexbird HTTP
API ($HEXBIRD_API_BASE, $HEXBIRD_TOKEN); without them it relies on the other-scene check.
It also flags possible azimuth-ambiguity ghosts: faint copies of a much brighter target ~5 km along track.

Detector (tuned on 99 AIS ships, 15,200 km2 of Danish waters, Oct 2026): ships >=100 m 47/47,
50-100 m 14/16, 15-50 m 19/26, <15 m 3/9; 1.5 unexplained detections per 1000 km2 (an upper bound
on false alarms: some are ships without AIS).
"""

import argparse, functools, json, math, os, re, sys, tempfile, warnings, xml.etree.ElementTree as ET
from datetime import datetime, timezone

import numpy as np
import requests
import rasterio
from pyproj import Transformer
from rasterio.warp import reproject, transform_bounds, Resampling
from rasterio.windows import Window, from_bounds
from scipy import ndimage
from scipy.interpolate import CubicSpline, LinearNDInterpolator, RegularGridInterpolator

# Remote COGs: don't list the directory or probe for sidecar files (.aux.xml, .msk, ...)
# on every open.
os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif,.tiff')

PC = 'https://planetarycomputer.microsoft.com/api'
CDSE = 'https://catalogue.dataspace.copernicus.eu/odata/v1/Products'
# $S1TOOL_CACHE, else the user cache dir ($XDG_CACHE_HOME or ~/.cache), else the temp dir
def _default_cache():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    try:
        os.makedirs(os.path.join(base, 's1tool'), exist_ok=True)
        if os.access(os.path.join(base, 's1tool'), os.W_OK):
            return os.path.join(base, 's1tool')
    except OSError:
        pass
    return os.path.join(tempfile.gettempdir(), 's1cache')


CACHE = os.environ.get('S1TOOL_CACHE') or _default_cache()
SHIPS_MAX_M = 40000     # largest box `ships` will scan
CROP_MAX_PX = 2000      # crops wider than 20 km are read downsampled
KT = 0.514444
# Detector settings from the AIS benchmark (see the module docstring).
MIN_VV_DB, MIN_CONTRAST_DB, MIN_VH_DB = -6.0, 8.0, -20.0
MERGE_M, FIXED_M, OSM_M, COAST_BUFFER_PX = 150.0, 120.0, 150.0, 60
# Ghost check (evals/sentinel1/ghosts.py: 2 of 23 benchmark false alarms were ghosts of big ships,
# ~30 dB fainter, within 10 m along / 31 m across of the predicted spot; a real boat near a
# predicted spot was 21 dB fainter and 124 m across).
GHOST_DB, GHOST_ALONG_M, GHOST_ACROSS_M, GHOST_MAX_DB = 25.0, 300.0, 100.0, 10.0
SEAMARK_SQL = '^(buoy_|beacon_|light|platform|pile|daymark|fog_signal)'
warnings.filterwarnings('ignore', category=RuntimeWarning)  # all-NaN blocks over land
HTTP = requests.Session()
HTTP.headers['User-Agent'] = 'hexbird-s1tool/0.1'


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-5] + 'Z'


def secs(s):
    s = s.rstrip('Z')
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def parse_time(s):
    if re.fullmatch(r'\d+(\.\d+)?', s):
        return float(s)
    return secs(s.replace(' ', 'T'))


def utm_epsg(lat, lon):
    return (32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1


def die(msg):
    sys.exit(msg)


# --- catalogue -------------------------------------------------------------------------------

_tokens = {}


def sign(href, collection):
    """Planetary Computer blob URLs need a short-lived SAS token (no account needed)."""
    if collection not in _tokens:
        r = HTTP.get(f'{PC}/sas/v1/token/{collection}', timeout=30)
        r.raise_for_status()
        _tokens[collection] = r.json()['token']
    return f"{href}?{_tokens[collection]}"


@functools.lru_cache(maxsize=None)
def stac_item(collection, item_id):
    r = HTTP.get(f'{PC}/stac/v1/collections/{collection}/items/{item_id}', timeout=30)
    return r.json() if r.ok else None


def stac_search(collection, lat, lon, start, end, limit=100):
    q = {'collections': [collection], 'intersects': {'type': 'Point', 'coordinates': [lon, lat]},
         'datetime': f'{start}/{end}', 'limit': limit}
    r = HTTP.post(f'{PC}/stac/v1/search', json=q, timeout=60)
    r.raise_for_status()
    return r.json().get('features', [])


def base_id(item):
    return item.removesuffix('_rtc')


def item_info(item):
    """The GRD item (always) and the RTC item (when processed)."""
    item = base_id(item)
    grd = stac_item('sentinel-1-grd', item)
    if not grd:
        die(f'{item}: no such Sentinel-1 GRD item on Planetary Computer (ids come from `scenes`)')
    return grd, stac_item('sentinel-1-rtc', item + '_rtc')


def cache_path(*parts):
    p = os.path.join(CACHE, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def annotation_xml(grd, kind='product', pol='vv'):
    """Product or calibration annotation XML of a GRD item (Planetary Computer renames the files)."""
    path = cache_path('annot', f"{grd['id']}_{kind}_{pol}.xml")
    if not os.path.exists(path):
        href = grd['assets'][f'schema-{kind}-{pol}']['href']
        if kind == 'product':  # the asset points at the RFI annotation; the product one sits beside it
            href = href.replace(f'annotation/rfi/rfi-iw-{pol}.xml', f'annotation/iw-{pol}.xml')
        r = HTTP.get(sign(href, 'sentinel-1-grd'), timeout=120)
        r.raise_for_status()
        open(path, 'w').write(r.text)
    return ET.parse(path).getroot()


# --- geometry: when a point was imaged, where a moving target appears -------------------------

class Geometry:
    def __init__(self, grd):
        root = annotation_xml(grd)
        g = root.findall('.//geolocationGridPoint')
        lat = np.array([float(p.findtext('latitude')) for p in g])
        lon = np.array([float(p.findtext('longitude')) for p in g])
        self.line = np.array([float(p.findtext('line')) for p in g])
        self.pixel = np.array([float(p.findtext('pixel')) for p in g])
        az = np.array([secs(p.findtext('azimuthTime')) for p in g])
        inc = np.array([float(p.findtext('incidenceAngle')) for p in g])
        self._az = LinearNDInterpolator(np.c_[lon, lat], az)
        self._inc = LinearNDInterpolator(np.c_[lon, lat], inc)
        orb = root.findall('.//orbitList/orbit')
        ot = np.array([secs(o.findtext('time')) for o in orb])
        self._pos = CubicSpline(ot, [[float(o.findtext(f'position/{c}')) for c in 'xyz'] for o in orb])
        self._vel = CubicSpline(ot, [[float(o.findtext(f'velocity/{c}')) for c in 'xyz'] for o in orb])
        self.direction = root.findtext('.//productInformation/pass') or ''
        self.ecef = Transformer.from_crs(4979, 4978, always_xy=True)
        # for azimuth-ambiguity ghosts: image line/pixel <-> lon/lat, PRF per subswath, wavelength
        self._lp = LinearNDInterpolator(np.c_[lon, lat], np.c_[self.line, self.pixel])
        self._ll = LinearNDInterpolator(np.c_[self.line, self.pixel], np.c_[lon, lat])
        self.prf = {d.findtext('swath'): float(d.findtext('prf')) for d in root.iter('downlinkInformation')}
        self.swaths = [(s.findtext('swath'), [(int(b.findtext('firstAzimuthLine')), int(b.findtext('lastAzimuthLine')),
                                               int(b.findtext('lastRangeSample'))) for b in s.iter('swathBounds')])
                       for s in root.iter('swathMerge')]
        f = root.findtext('.//productInformation/radarFrequency')
        self.wavelength = 299_792_458.0 / float(f) if f else 0.0555

    def time(self, lat, lon):
        return float(self._az(lon, lat))

    def incidence(self, lat, lon):
        return float(self._inc(lon, lat))

    def look(self, lat, lon):
        """Imaging time, incidence, slant range, the ground track direction (deg) and the
        direction the radar looks (deg, from the satellite towards the ground)."""
        t = self.time(lat, lon)
        if math.isnan(t):
            return None
        p = np.array(self.ecef.transform(lon, lat, 0.0))
        s, vs = self._pos(t), self._vel(t)
        e, n, u = _enu(lat, lon)
        los = (p - s) / np.linalg.norm(p - s)
        trk = vs - (vs @ u) * u
        lk = los - (los @ u) * u
        return {'t': t, 'inc': self.incidence(lat, lon), 'R': float(np.linalg.norm(p - s)),
                'V': float(np.linalg.norm(vs)), 'los': los, 'e': e, 'n': n,
                'track_deg': math.degrees(math.atan2(trk @ e, trk @ n)) % 360,
                'look_deg': math.degrees(math.atan2(lk @ e, lk @ n)) % 360}

    def _line_pixel(self, lat, lon):
        lp = np.ravel(self._lp(lon, lat))
        return (float(lp[0]), float(lp[1])) if np.isfinite(lp).all() else None

    def along_line(self, lat, lon):
        """Unit (east, north) vector along the image lines (azimuth, the direction the satellite
        images in) at a point, from the tie-point grid. Moving-target shifts and ghosts lie along it;
        it is ~1 deg off the satellite's ground track."""
        lp = self._line_pixel(lat, lon)
        for dl in (200, -200) if lp else ():
            lo2, la2 = (float(v) for v in np.ravel(self._ll(lp[0] + dl, lp[1])))
            if np.isfinite(lo2) and np.isfinite(la2):
                v = math.copysign(1, dl) * np.array([(lo2 - lon) * math.cos(math.radians(lat)), la2 - lat])
                return v / np.linalg.norm(v)
        return None

    def shift(self, lat, lon, sog_kt, cog_deg):
        """Where SAR puts a target moving at sog/cog: displaced along the image lines by -R*v_range/V
        (receding targets appear behind, approaching ones ahead). Returns (dE, dN, v_range m/s)."""
        g = self.look(lat, lon)
        v = sog_kt * KT * (math.sin(math.radians(cog_deg)) * g['e'] + math.cos(math.radians(cog_deg)) * g['n'])
        vr = float(v @ g['los'])
        d = -g['R'] * vr / g['V']
        ue, un = self.along_line(lat, lon)
        return d * ue, d * un, vr

    def ambiguity(self, lat, lon, to_xy):
        """Azimuth-ambiguity ('ghost') geometry at a point: a bright target makes faint copies
        D = PRF * wavelength * R / (2 V) along the image lines either side of it (~4.6-5.8 km in IW,
        typically ~30 dB fainter). Returns (subswath, D m, unit vector along the lines in the
        projected CRS of the `to_xy` transformer)."""
        g, lp, ul = self.look(lat, lon), self._line_pixel(lat, lon), self.along_line(lat, lon)
        if g is None or lp is None or ul is None:
            return None
        swath = next((name for name, bounds in self.swaths for first, last, last_px in bounds
                      if first <= lp[0] <= last and lp[1] <= last_px), self.swaths[-1][0] if self.swaths else None)
        if swath not in self.prf:
            return None
        D = self.prf[swath] * self.wavelength * g['R'] / (2 * g['V'])
        la2, lo2 = offset(lat, lon, 1000 * ul[0], 1000 * ul[1])
        u = np.subtract(to_xy.transform(lo2, la2), to_xy.transform(lon, lat))
        return swath, D, u / np.linalg.norm(u)


def _enu(lat, lon):
    la, lo = math.radians(lat), math.radians(lon)
    e = np.array([-math.sin(lo), math.cos(lo), 0])
    n = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    u = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    return e, n, u


def offset(lat, lon, de, dn):
    return lat + dn / 111_320, lon + de / (111_320 * math.cos(math.radians(lat)))


# --- imagery ---------------------------------------------------------------------------------

def read_box(item, lat, lon, size, pols=('vv', 'vh'), res=10.0):
    """gamma0 (linear) on a north-up UTM grid centred on the point. RTC when processed, else GRD
    geocoded from its tie points. Cached."""
    grd, rtc = item_info(item)
    if os.environ.get('S1TOOL_SOURCE', '').lower() == 'grd':  # testing: geocode GRD even when RTC exists
        rtc = None
    key = cache_path('img', f"{grd['id']}_{'rtc' if rtc else 'grd'}_{lat:.5f}_{lon:.5f}_{int(size)}_{int(res)}_{'_'.join(pols)}.npz")
    if os.path.exists(key):
        z = np.load(key, allow_pickle=True)
        out = {p: z[p] for p in pols}
        out.update(transform=rasterio.Affine(*z['transform'][:6]), crs=rasterio.crs.CRS.from_epsg(int(z['epsg'])),
                   source=str(z['source']), id=grd['id'], time=grd['properties']['datetime'])
        return out
    epsg = utm_epsg(lat, lon)
    x, y = Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)
    n = int(round(size / res))
    dst_t = rasterio.Affine(res, 0, x - size / 2, 0, -res, y + size / 2)
    out = {'transform': dst_t, 'crs': rasterio.crs.CRS.from_epsg(epsg), 'id': grd['id'],
           'time': grd['properties']['datetime']}
    if rtc:
        out['source'] = 'RTC'
        for p in pols:
            dst = np.zeros((n, n), np.float32)
            with rasterio.open(sign(rtc['assets'][p]['href'], 'sentinel-1-rtc')) as src:
                w = from_bounds(*transform_bounds(out['crs'], src.crs, x - size / 2, y - size / 2,
                                                  x + size / 2, y + size / 2), src.transform)
                w = w.round_offsets().round_lengths()
                w = Window(w.col_off - 4, w.row_off - 4, w.width + 8, w.height + 8)
                arr = src.read(1, window=w, boundless=True, fill_value=0).astype(np.float32)
                reproject(arr, dst, src_transform=src.window_transform(w), src_crs=src.crs, src_nodata=0,
                          dst_transform=dst_t, dst_crs=out['crs'], dst_nodata=0, resampling=Resampling.bilinear)
            out[p] = dst
    else:
        out['source'] = 'GRD'
        for p in pols:
            out[p] = _grd_box(grd, p, lat, lon, size, dst_t, out['crs'], n)
    np.savez_compressed(key, transform=np.array(tuple(dst_t)), epsg=epsg, source=out['source'],
                        **{p: out[p] for p in pols})
    return out


def _grd_box(grd, pol, lat, lon, size, dst_t, dst_crs, n):
    """GRD (radar geometry, DN) -> gamma0 on the destination grid: read only the source window the
    box needs, calibrate with the annotation's gamma LUT, warp with the tie points in and near it."""
    from rasterio.control import GroundControlPoint
    from rasterio.transform import GCPTransformer
    with rasterio.open(sign(grd['assets'][pol]['href'], 'sentinel-1-grd')) as src:
        gcps, gcrs = src.gcps
        tr = GCPTransformer(gcps)
        dlat = size / 2 / 111_320 * 1.1
        dlon = dlat / math.cos(math.radians(lat))
        lons = [lon - dlon, lon + dlon, lon - dlon, lon + dlon, lon]
        lats = [lat - dlat, lat - dlat, lat + dlat, lat + dlat, lat]
        rows, cols = tr.rowcol(lons, lats)
        r0, r1 = max(min(rows) - 50, 0), min(max(rows) + 50, src.height)
        c0, c1 = max(min(cols) - 50, 0), min(max(cols) + 50, src.width)
        if r1 <= r0 or c1 <= c0:
            return np.zeros((n, n), np.float32)
        dn = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0)).astype(np.float32)
    lut = _gamma_lut(grd, pol)
    rr, cc = np.mgrid[r0:r1, c0:c1]
    g = lut((rr.ravel(), cc.ravel())).reshape(dn.shape)
    gamma0 = np.where(dn > 0, dn ** 2 / g ** 2, 0).astype(np.float32)
    # tie points near the window, shifted into window coordinates (a local fit is accurate). The
    # grid is ~2000 lines x ~1300 pixels, so a small box can see just one or two rows of it: widen
    # to 3 rows and columns, and cap the polynomial order below the number of distinct rows and
    # columns (GDAL picks the order from the point count alone, and a 2-row fit of order 2+ fails).
    for m in (1500, 3000, 6000, 12000):
        local = [GroundControlPoint(row=q.row - r0, col=q.col - c0, x=q.x, y=q.y, z=q.z) for q in gcps
                 if r0 - m <= q.row <= r1 + m and c0 - m <= q.col <= c1 + m]
        nr, nc = len({q.row for q in local}), len({q.col for q in local})
        if nr >= 3 and nc >= 3:
            break
    order = min(3, nr - 1, nc - 1)
    dst = np.zeros((n, n), np.float32)
    reproject(gamma0, dst, gcps=local, src_crs=gcrs, src_nodata=0, dst_transform=dst_t, dst_crs=dst_crs,
              dst_nodata=0, resampling=Resampling.bilinear, **({'MAX_GCP_ORDER': order} if order < 3 else {}))
    return dst


def _gamma_lut(grd, pol):
    root = annotation_xml(grd, 'calibration', pol)
    vecs = root.findall('.//calibrationVector')
    lines = np.array([float(v.findtext('line')) for v in vecs])
    pix = np.array([float(x) for x in vecs[0].findtext('pixel').split()])
    vals = np.array([[float(x) for x in v.findtext('gamma').split()] for v in vecs])
    return RegularGridInterpolator((lines, pix), vals, bounds_error=False, fill_value=None)


def land_mask(img):
    """Land from the Copernicus GLO-30 DEM (ocean is 0 m there) on the image grid, buffered."""
    H, W = img['vv'].shape
    t, crs = img['transform'], img['crs']
    key = cache_path('dem', f"{crs.to_epsg()}_{t.c:.0f}_{t.f:.0f}_{H}_{W}.npy")
    if os.path.exists(key):
        return np.load(key)
    west, south, east, north = transform_bounds(crs, 4326, t.c, t.f + t.e * H, t.c + t.a * W, t.f)
    dem = np.zeros((H, W), np.float32)
    for la in range(math.floor(south), math.floor(north) + 1):
        for lo in range(math.floor(west), math.floor(east) + 1):
            name = (f"Copernicus_DSM_COG_10_{'N' if la >= 0 else 'S'}{abs(la):02d}_00_"
                    f"{'E' if lo >= 0 else 'W'}{abs(lo):03d}_00_DEM")
            url = f'https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif'
            if HTTP.head(url, timeout=20).status_code != 200:
                continue  # no tile: all sea
            part = np.zeros((H, W), np.float32)
            with rasterio.open(url) as src:
                reproject(rasterio.band(src, 1), part, dst_transform=t, dst_crs=crs)
            dem = np.maximum(dem, part)
    land = ndimage.binary_dilation(dem > 0.5, iterations=COAST_BUFFER_PX)
    np.save(key, land)
    return land


# --- detection -------------------------------------------------------------------------------

def detect(img, land, min_db=MIN_VV_DB, min_contrast_db=MIN_CONTRAST_DB):
    """Bright compact targets on open water: contrast against the median of water pixels in
    500 m blocks; blobs merged within MERGE_M (one ship can split into several)."""
    vv = img['vv']
    water = ndimage.binary_erosion(vv > 0, iterations=20) & ~land
    b = 50
    H, W = vv.shape
    hb, wb = -(-H // b), -(-W // b)
    pad = np.full((hb * b, wb * b), np.nan, np.float32)
    pad[:H, :W] = np.where(water, vv, np.nan)
    with np.errstate(all='ignore'):
        blk = np.nanmedian(pad.reshape(hb, b, wb, b).transpose(0, 2, 1, 3).reshape(hb, wb, -1), axis=2)
        blk = np.where(np.isnan(blk), np.nanmedian(blk) if np.isfinite(blk).any() else 1.0, blk)
    bg = np.kron(blk, np.ones((b, b), np.float32))[:H, :W]
    with np.errstate(all='ignore'):
        ratio = np.where(water, vv / bg, 0)
    mask = (ratio > 10 ** (min_contrast_db / 10)) & (vv > 10 ** (min_db / 10))
    lab, _ = ndimage.label(ndimage.binary_dilation(mask, iterations=2))
    to_ll = Transformer.from_crs(img['crs'], 4326, always_xy=True)
    vh = img.get('vh')
    dets = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        m = (lab[sl] == i) & mask[sl]
        if not m.any():
            continue
        w = vv[sl] * m
        cy, cx = ndimage.center_of_mass(w)
        x, y = img['transform'] * (sl[1].start + cx + 0.5, sl[0].start + cy + 0.5)
        lo, la = to_ll.transform(x, y)
        ys, xs = np.nonzero(m)
        L, hdg = _axis(xs, ys, vv[sl][m])
        d = {'lat': round(la, 5), 'lon': round(lo, 5), 'x': x, 'y': y, 'px': int(m.sum()),
             'length_m': round(L), 'axis_deg': hdg,
             'vv_db': round(float(10 * np.log10(vv[sl][m].max())), 1),
             'contrast_db': round(float(10 * np.log10(ratio[sl][m].max())), 1)}
        if vh is not None:
            d['vh_db'] = round(float(10 * np.log10(vh[sl][m].max() + 1e-9)), 1)
        dets.append(d)
    dets.sort(key=lambda d: -d['vv_db'])
    kept = []
    for d in dets:
        if not any(_same_target(k, d) for k in kept):
            kept.append(d)
    return kept, water


def _same_target(k, d):
    """d is part of the brighter target k: within MERGE_M, or along k's streak within its extent
    (long ships smear into several blobs)."""
    dx, dy = d['x'] - k['x'], d['y'] - k['y']
    r = math.hypot(dx, dy)
    if r <= MERGE_M:
        return True
    if k['axis_deg'] is None or k['length_m'] < 2 * MERGE_M:
        return False
    a = math.radians(k['axis_deg'])
    along = abs(dx * math.sin(a) + dy * math.cos(a))
    across = abs(dx * math.cos(a) - dy * math.sin(a))
    return along <= k['length_m'] / 2 + MERGE_M and across <= 60


def _axis(xs, ys, w):
    """Extent along the blob's main axis (m, includes sidelobes and smear) and its direction."""
    if len(xs) < 3:
        return 10.0 * max(1, len(xs)), None
    w = w / w.sum()
    mx, my = (xs * w).sum(), (ys * w).sum()
    cov = np.cov(np.vstack([xs - mx, ys - my]), aweights=w)
    ev, evec = np.linalg.eigh(cov)
    v = evec[:, 1]
    proj = (xs - mx) * v[0] + (ys - my) * v[1]
    return 10.0 * (proj.max() - proj.min() + 1), round(math.degrees(math.atan2(v[0], -v[1])) % 180)


def compare_scenes(item, lat, lon, n=3, days=36):
    """Up to n earlier scenes of the point, one per date, same relative orbit first (same viewing
    direction, so tall structures are displaced the same way)."""
    grd, _ = item_info(item)
    t0 = grd['properties']['datetime'][:10]
    start = datetime.fromtimestamp(secs(t0 + 'T00:00:00') - days * 86400, timezone.utc).strftime('%Y-%m-%dT00:00:00Z')
    feats = stac_search('sentinel-1-grd', lat, lon, start, t0 + 'T00:00:00Z')
    me = grd['properties'].get('sat:relative_orbit')
    same = [f for f in feats if f['properties'].get('sat:relative_orbit') == me]
    other = [f for f in feats if f['properties'].get('sat:relative_orbit') != me]
    out, dates = [], set()
    for f in sorted(same, key=lambda f: f['properties']['datetime'], reverse=True) + \
            sorted(other, key=lambda f: f['properties']['datetime'], reverse=True):
        d = f['properties']['datetime'][:10]
        if d not in dates and f['properties'].get('sar:instrument_mode') == 'IW':
            out.append(f['id'])
            dates.add(d)
        if len(out) == n:
            break
    return out


def mark_fixed(dets, others):
    for d in dets:
        d['seen_in'] = 0
    for o in others:
        for d in dets:
            if any(math.hypot((d['lat'] - e['lat']) * 111_320,
                              (d['lon'] - e['lon']) * 111_320 * math.cos(math.radians(d['lat']))) < FIXED_M
                   for e in o):
                d['seen_in'] += 1
    for d in dets:
        d['fixed'] = bool(others) and d['seen_in'] * 2 >= len(others)


def hexbird_osm(sql):
    base = os.environ.get('HEXBIRD_API_BASE', 'https://hexbird.io').rstrip('/')
    tok = os.environ.get('HEXBIRD_TOKEN')
    if not tok:
        raise RuntimeError('HEXBIRD_TOKEN is not set')
    r = HTTP.post(f'{base}/api/v1/tools/query_openstreetmap', headers={'Authorization': f'Bearer {tok}'},
                  json={'sql': sql}, timeout=300)
    if not r.ok:
        raise RuntimeError(f'Hexbird OSM query failed: {r.status_code} {r.text[:200]}')
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def osm_structures(lat, lon, size, depth=0):
    """Mapped fixed structures at sea as (lat, lon, kind): bridges, piers, breakwaters, groynes,
    platforms, wind turbines, power lines and towers, lighthouses, and point seamarks (buoys,
    beacons, lights; not areas such as anchorages). Lines sampled every 50 m."""
    key = cache_path('osm', f'{lat:.5f}_{lon:.5f}_{int(size)}.json')
    if depth == 0 and os.path.exists(key):
        return [tuple(p) for p in json.load(open(key))]
    near = (f"ST_DWithin(way::geography, ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326)::geography, "
            f"{size / 2 * 1.42:.0f})")
    kind = ("COALESCE(CASE WHEN power = 'generator' THEN 'turbine/generator' END, "
            "'seamark:' || (tags->'seamark:type'), man_made, CASE WHEN bridge IS NOT NULL THEN 'bridge' END, "
            "power, 'structure')")
    dump = '(ST_DumpPoints(ST_Segmentize({g}::geography, 50)::geometry)).geom AS p'
    queries = [
        f"SELECT {kind} AS kind, ST_Y(p) AS lat, ST_X(p) AS lon FROM (SELECT tags, man_made, bridge, power, "
        f"{dump.format(g='way')} FROM planet_osm_line WHERE {near} AND (bridge IS NOT NULL "
        f"OR man_made IN ('pier', 'breakwater', 'groyne') OR power = 'line')) s",
        f"SELECT {kind} AS kind, ST_Y(p) AS lat, ST_X(p) AS lon FROM (SELECT tags, man_made, bridge, power, "
        f"{dump.format(g='ST_Boundary(way)')} FROM planet_osm_polygon WHERE {near} AND (bridge IS NOT NULL "
        f"OR man_made IN ('pier', 'breakwater', 'groyne', 'offshore_platform', 'bridge') "
        f"OR power IN ('generator', 'platform', 'substation'))) s",
        f"SELECT {kind} AS kind, ST_Y(way) AS lat, ST_X(way) AS lon FROM planet_osm_point WHERE {near} AND ("
        f"man_made IN ('offshore_platform', 'lighthouse', 'mast', 'tower') "
        f"OR power IN ('tower', 'generator', 'transformer', 'platform', 'substation') "
        f"OR (tags->'seamark:type' ~ '{SEAMARK_SQL}' AND tags->'seamark:type' NOT LIKE '%mooring%'))",
    ]
    pts = []
    for q in queries:
        rows = hexbird_osm(q)
        if len(rows) >= 10_000 and depth < 3:  # the API caps results: split the box
            h = size / 4
            dl, dlo = h / 111_320, h / (111_320 * math.cos(math.radians(lat)))
            pts = list({p for sl in (-1, 1) for so in (-1, 1)
                        for p in osm_structures(round(lat + sl * dl, 5), round(lon + so * dlo, 5), size / 2, depth + 1)})
            break
        pts += [(round(float(r['lat']), 6), round(float(r['lon']), 6), r['kind']) for r in rows]
    if depth == 0:
        json.dump(pts, open(key, 'w'))
    return pts


def mark_osm(dets, pts):
    if not pts:
        return
    P = np.array([(p[0], p[1]) for p in pts])
    for d in dets:
        dist = np.hypot((P[:, 0] - d['lat']) * 111_320, (P[:, 1] - d['lon']) * 111_320 * math.cos(math.radians(d['lat'])))
        i = int(np.argmin(dist))
        if dist[i] < OSM_M:
            d['osm'] = str(pts[i][2]).removeprefix('seamark:')


def _sample_vv(im, x, y, crs=None):
    """VV at points of an image dict; NaN outside it. Points are in `crs` (default: the image's)."""
    if crs is not None and crs != im['crs']:
        x, y = Transformer.from_crs(crs, im['crs'], always_xy=True).transform(x, y)
    c, r = ~im['transform'] * (x, y)
    r, c = np.floor(r).astype(int), np.floor(c).astype(int)
    H, W = im['vv'].shape
    ok = (r >= 0) & (r < H) & (c >= 0) & (c < W)
    out = np.full(r.shape, np.nan, np.float32)
    out[ok] = im['vv'][r[ok], c[ok]]
    return out


def flag_label(d):
    """Why a detection is not a candidate ship ('' when it is one)."""
    return 'fixed' if d.get('fixed') else d['osm'] if 'osm' in d else 'ghost?' if 'ghost' in d else ''


def mark_ghosts(dets, img, geo, item):
    """Flag candidates one ambiguity distance along the image lines from a target >= GHOST_DB
    brighter. The bright target may be outside the box or on land (port cranes, bridges): then a
    1 km window is read there (0.4-7 s; a box padded by the ghost distance costs 20-40 s for 40 km,
    and few candidates need a window)."""
    to_xy = Transformer.from_crs(4326, img['crs'], always_xy=True)
    to_ll = Transformer.from_crs(img['crs'], 4326, always_xy=True)
    S, T = (v.ravel() for v in np.meshgrid(np.arange(-GHOST_ALONG_M, GHOST_ALONG_M + 1, 10.0),
                                           np.arange(-GHOST_ACROSS_M, GHOST_ACROSS_M + 1, 10.0)))
    for d in dets:
        if flag_label(d) or d['vv_db'] > GHOST_MAX_DB:
            continue
        amb = geo.ambiguity(d['lat'], d['lon'], to_xy)
        if amb is None:
            continue
        swath, D, u = amb
        w = np.array([-u[1], u[0]])
        for k in (1, -1):
            x, y = d['x'] + (k * D + S) * u[0] + T * w[0], d['y'] + (k * D + S) * u[1] + T * w[1]
            v = _sample_vv(img, x, y)
            if np.isnan(v).any():
                clo, cla = to_ll.transform(d['x'] + k * D * u[0], d['y'] + k * D * u[1])
                sub = read_box(item, round(cla, 5), round(clo, 5), 2 * GHOST_ALONG_M + 400, pols=('vv',))
                v = np.where(np.isnan(v), _sample_vv(sub, x, y, img['crs']), v)
            v = np.nan_to_num(v)
            j = int(np.argmax(v))
            db = 10 * math.log10(v[j]) if v[j] > 0 else -99.0
            if db - d['vv_db'] >= GHOST_DB and db > d.get('ghost', {}).get('of_db', -99):
                glo, gla = to_ll.transform(x[j], y[j])
                d['ghost'] = {'of_lat': round(gla, 5), 'of_lon': round(glo, 5), 'of_db': round(db, 1),
                              'dist_m': round(D + k * S[j]), 'swath': swath}
                # the bright target among our detections (its blob or its streak)?
                d['_ghost_of'] = next((e for e in dets if e is not d and _same_target(e, {'x': x[j], 'y': y[j]})), None)


# --- commands --------------------------------------------------------------------------------

def cmd_scenes(a):
    start, end = a.start, a.end
    start = start if 'T' in start else start + 'T00:00:00Z'
    end = end if 'T' in end else end + 'T23:59:59Z'
    feats = stac_search('sentinel-1-grd', a.lat, a.lon, start, end)
    feats = [f for f in feats if f['properties'].get('sar:instrument_mode') == 'IW']
    rtc = {f['id'].removesuffix('_rtc') for f in stac_search('sentinel-1-rtc', a.lat, a.lon, start, end)}
    feats.sort(key=lambda f: f['properties']['datetime'])
    print(f"Sentinel-1 IW passes over {a.lat}, {a.lon}, {start[:10]} to {end[:10]} (Planetary Computer):")
    if not feats:
        print('  none')
    for f in feats:
        p = f['properties']
        g = Geometry(f).look(a.lat, a.lon) if len(feats) <= 20 else None
        when = (f"imaged here {iso(g['t'])}, incidence {g['inc']:.0f} deg, radar looking {g['look_deg']:.0f} deg"
                if g else f"scene {p['datetime'][:19]}Z")
        print(f"  {f['id']}  {p.get('sat:orbit_state', '?')} orbit {p.get('sat:relative_orbit')}  {when}  "
              f"[{'RTC' if f['id'] in rtc else 'GRD only'}]")
    # passes on Copernicus Data Space that Planetary Computer hasn't ingested yet
    try:
        flt = (f"Collection/Name eq 'SENTINEL-1' and contains(Name,'_IW_GRDH_') and "
               f"OData.CSC.Intersects(area=geography'SRID=4326;POINT({a.lon} {a.lat})') and "
               f"ContentDate/Start gt {start[:19]}.000Z and ContentDate/Start lt {end[:19]}.000Z")
        r = HTTP.get(CDSE, params={'$filter': flt, '$top': 100, '$orderby': 'ContentDate/Start'}, timeout=60)
        have = {f['properties']['datetime'][:16] for f in feats}
        extra = sorted({v['ContentDate']['Start'][:19] for v in r.json().get('value', [])
                        if v['ContentDate']['Start'][:16] not in have and 'COG' not in v['Name']})
        if extra:
            print('Also on Copernicus Data Space, not yet on Planetary Computer (try again in a few hours):')
            for t in extra:
                print(f'  {t}Z')
    except (requests.RequestException, ValueError):
        pass
    print('Sentinel-1 passes near 06:00 (descending) and 18:00 (ascending) local solar time, never at '
          "Sentinel-2's ~10:30; S1C and S1D together see a place every 1-3 days.")


def cmd_when(a):
    grd, rtc = item_info(a.item)
    geo = Geometry(grd)
    g = geo.look(a.lat, a.lon)
    if not g:
        die(f'{a.lat}, {a.lon} is outside {grd["id"]}')
    print(f"{grd['id']} ({geo.direction.lower()}, {'RTC' if rtc else 'GRD only'})")
    print(f"  imaged at {iso(g['t'])} (to ~0.01 s), incidence {g['inc']:.1f} deg, slant range {g['R'] / 1000:.0f} km")
    print(f"  satellite ground track {g['track_deg']:.0f} deg, radar looking towards {g['look_deg']:.0f} deg")
    print(f"  a moving target is displaced {g['R'] / g['V']:.0f} m along track per 1 m/s of its speed along the "
          f"radar's line of sight: receding targets towards {(g['track_deg'] + 180) % 360:.0f} deg, "
          f"approaching ones towards {g['track_deg']:.0f} deg")
    if a.sog is not None:
        la, lo = a.lat, a.lon
        if a.at:
            dt = g['t'] - parse_time(a.at)
            d = a.sog * KT * dt
            la, lo = offset(la, lo, d * math.sin(math.radians(a.cog)), d * math.cos(math.radians(a.cog)))
            print(f"  dead-reckoned {dt / 60:+.1f} min from {a.at}: {la:.5f}, {lo:.5f}")
        de, dn, vr = geo.shift(la, lo, a.sog, a.cog)
        pla, plo = offset(la, lo, de, dn)
        print(f"  at {a.sog} kt on {a.cog} deg: range rate {vr:+.1f} m/s, appears {math.hypot(de, dn):.0f} m "
              f"away, at {pla:.5f}, {plo:.5f}")


def cmd_ships(a):
    if a.size > SHIPS_MAX_M:
        die(f'SIZE_M is at most {SHIPS_MAX_M} m: pick the area from the clues')
    os.makedirs(a.out, exist_ok=True)
    img = read_box(a.item, a.lat, a.lon, a.size)
    land = land_mask(img)
    dets, water = detect(img, land, min_db=a.min_db)
    if 'vh' in img and a.min_vh_db > -99:
        dets = [d for d in dets if d.get('vh_db', 0) >= a.min_vh_db]
    geo = Geometry(item_info(a.item)[0])
    for d in dets:
        d['time'] = iso(geo.time(d['lat'], d['lon']))
    others = compare_scenes(a.item, a.lat, a.lon, n=a.compare) if a.compare else []
    odets = []
    for o in others:
        oi = read_box(o, a.lat, a.lon, a.size, pols=('vv',))
        odets.append(detect(oi, land if oi['transform'] == img['transform'] else land_mask(oi), min_db=-6)[0])
    mark_fixed(dets, odets)
    osm_note = ''
    try:
        mark_osm(dets, osm_structures(a.lat, a.lon, a.size))
    except (RuntimeError, requests.RequestException) as e:
        osm_note = f' (OSM structures unavailable: {e})'
    mark_ghosts(dets, img, geo, a.item)
    area = float(water.sum()) * 1e-4
    ships = [d for d in dets if not flag_label(d)]
    print(f"{img['id']} [{img['source']}], {a.size / 1000:.0f} km box at {a.lat}, {a.lon}: "
          f"{area:.0f} km2 of open water")
    flagged = len(dets) - len(ships)
    print(f"{len(dets)} bright targets: {len(ships)} candidate ships; {flagged} flagged "
          f"({sum(d['fixed'] for d in dets)} also in other passes: fixed, or a ship anchored for days; "
          f"{sum('osm' in d for d in dets)} at a mapped structure; {sum('ghost' in d for d in dets)} possible "
          f"radar ghosts of a much brighter target){osm_note}")
    dets = ships + [d for d in dets if d not in ships]  # candidates first, each group brightest first
    print(f"compared with: {', '.join(o[:33] for o in others) or 'none'}")
    for i, d in enumerate(dets, 1):
        d['n'] = i
    for d in dets:
        if d.get('_ghost_of'):
            d['ghost']['of_n'] = d['_ghost_of']['n']
        d.pop('_ghost_of', None)
    for d in dets[:a.top]:
        flags = []
        if d['fixed']:
            flags.append(f"in {d['seen_in']}/{len(others)} other passes")
        if 'osm' in d:
            flags.append(f"at mapped {d['osm']}")
        if 'ghost' in d:
            g = d['ghost']
            of = f" (#{g['of_n']})" if 'of_n' in g else ''
            flags.append(f"possible ghost of the {g['of_db']:+.0f} dB target{of} {g['dist_m'] / 1000:.2f} km along track "
                         f"at {g['of_lat']:.5f}, {g['of_lon']:.5f}")
        axis = '' if d['axis_deg'] is None else f" along {d['axis_deg']} deg"
        flag = '  [' + '; '.join(flags) + ']' if flags else ''
        print(f"  #{d['n']:<2} {d['lat']:.5f}, {d['lon']:.5f}  {d['time'][11:22]}  VV {d['vv_db']:+.1f} dB, "
              f"VH {d.get('vh_db', float('nan')):+.1f} dB, extent {d['length_m']} m{axis}{flag}")
    for d in dets:
        d.pop('x'), d.pop('y')
    res = {'item': img['id'], 'source': img['source'], 'box': [a.lat, a.lon, a.size], 'water_km2': round(area),
           'compare': others, 'detections': dets}
    json.dump(res, open(os.path.join(a.out, 's1_ships.json'), 'w'), indent=1)
    sheet = _contact_sheet(img, dets[:a.top], os.path.join(a.out, 's1_ships.png'))
    print(f'wrote {sheet} and s1_ships.json in {a.out}')


def _contact_sheet(img, dets, path):
    """Overview of the box (left, numbered) and 1 km VV/VH chips of each target (right)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    pairs = 4
    rows = max(3, -(-len(dets) // pairs))
    fig = plt.figure(figsize=(1.9 * (rows + 2 * pairs), 1.9 * rows + 0.4))
    gs = fig.add_gridspec(rows, rows + 2 * pairs, wspace=0.05, hspace=0.25)
    ax = fig.add_subplot(gs[:, :rows])
    db = 10 * np.log10(np.maximum(img['vv'], 1e-5))
    t = img['transform']
    H, W = db.shape
    ax.imshow(db, cmap='gray', vmin=-25, vmax=5, extent=[t.c, t.c + t.a * W, t.f + t.e * H, t.f])
    to_xy = Transformer.from_crs(4326, img['crs'], always_xy=True)
    for d in dets:
        x, y = to_xy.transform(d['lon'], d['lat'])
        col = 'cyan' if flag_label(d) else 'red'
        ax.plot(x, y, 'o', mfc='none', mec=col, ms=10, mew=1.2)
        ax.annotate(str(d['n']), (x, y), xytext=(6, 6), textcoords='offset points', color='yellow', fontsize=9)
    ax.set_xticks([]), ax.set_yticks([])
    ax.set_title(f"{img['id'][:32]} {img['time'][:16]}Z VV, north up\nred: candidate ship, "
                 f"cyan: also in other passes, at a mapped structure, or a possible ghost", fontsize=9)
    for i, d in enumerate(dets):
        x, y = to_xy.transform(d['lon'], d['lat'])
        c, r = ~t * (x, y)
        r, c = int(r), int(c)
        for j, (pol, lo, hi) in enumerate((('vv', -22, 12), ('vh', -30, 2))):
            if pol not in img:
                continue
            a = img[pol][max(r - 50, 0):r + 50, max(c - 50, 0):c + 50]
            ax2 = fig.add_subplot(gs[i // pairs, rows + 2 * (i % pairs) + j])
            ax2.imshow(10 * np.log10(np.maximum(a, 1e-5)), cmap='gray', vmin=lo, vmax=hi)
            ax2.set_xticks([]), ax2.set_yticks([])
            flag = f' {flag_label(d)}' if flag_label(d) else ''
            ax2.set_title(f"#{d['n']} VV{flag}" if j == 0 else 'VH', fontsize=8)
    fig.text(0.99, 0.005, f"chips 1 km. Contains modified Copernicus Sentinel data {img['time'][:4]}",
             ha='right', fontsize=8)
    fig.savefig(path, dpi=100, bbox_inches='tight')
    plt.close(fig)
    return path


def cmd_crop(a):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    res = max(10.0, a.size / CROP_MAX_PX)
    fig, axes = plt.subplots(1, len(a.items), figsize=(7 * len(a.items), 7.4), squeeze=False)
    for ax, it in zip(axes[0], a.items):
        img = read_box(it, a.lat, a.lon, a.size, pols=(a.pol,), res=res)
        db = 10 * np.log10(np.maximum(img[a.pol], 1e-5))
        ax.imshow(db, cmap='gray', vmin=-25 if a.pol == 'vv' else -32, vmax=5 if a.pol == 'vv' else 0)
        ax.set_xticks([]), ax.set_yticks([])
        ax.set_title(f"{img['id'][:32]} {a.pol.upper()} [{img['source']}] {res:.0f} m/px", fontsize=9)
    fig.suptitle(f"{a.size / 1000:g} km at {a.lat}, {a.lon} (north up). Contains modified Copernicus "
                 f"Sentinel data", fontsize=9)
    fig.tight_layout()
    out = a.out or f's1_crop_{a.lat}_{a.lon}.png'
    fig.savefig(out, dpi=100)
    print(f'wrote {out}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scenes', help='passes over a point and when it was imaged')
    s.add_argument('lat', type=float), s.add_argument('lon', type=float), s.add_argument('start'), s.add_argument('end')
    s.set_defaults(fn=cmd_scenes)
    s = sub.add_parser('when', help="imaging time and geometry at a point; where a moving ship appears")
    s.add_argument('item'), s.add_argument('lat', type=float), s.add_argument('lon', type=float)
    s.add_argument('--sog', type=float, help='ship speed, kt'), s.add_argument('--cog', type=float, default=0.0)
    s.add_argument('--at', help='time of LAT LON (ISO); dead-reckons to the imaging time')
    s.set_defaults(fn=cmd_when)
    s = sub.add_parser('ships', help='ship candidates in a box, with fixed-object flags')
    s.add_argument('item'), s.add_argument('lat', type=float), s.add_argument('lon', type=float)
    s.add_argument('size', type=float)
    s.add_argument('--compare', type=int, default=3, help='earlier passes to find fixed objects with (0: none)')
    s.add_argument('--min-db', type=float, default=MIN_VV_DB, help='peak VV floor; -12 finds more small boats')
    s.add_argument('--min-vh-db', type=float, default=MIN_VH_DB)
    s.add_argument('--top', type=int, default=24), s.add_argument('--out', default='s1_ships')
    s.set_defaults(fn=cmd_ships)
    s = sub.add_parser('crop', help='images to look at')
    s.add_argument('lat', type=float), s.add_argument('lon', type=float), s.add_argument('size', type=float)
    s.add_argument('items', nargs='+'), s.add_argument('--pol', choices=['vv', 'vh'], default='vv')
    s.add_argument('--out')
    s.set_defaults(fn=cmd_crop)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
