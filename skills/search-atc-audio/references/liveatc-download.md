# Downloading LiveATC archive audio

Recipe that worked (Oct 2026). LiveATC keeps ~7 days of 30-minute archive files per feed.

## Why not curl

- `www.liveatc.net` and `archive.liveatc.net` sit behind Cloudflare; curl/requests get a
  403 "Sorry, you have been blocked" page.
- Archive files need a **signed link** (`...mp3?md5=...&expires=...`) issued by
  `archive-response.php`. A bare `https://archive.liveatc.net/<icao>/<file>.mp3` returns
  an HTML "Link Required" page. The form POST also needs a Cloudflare Turnstile token, so
  a scripted `fetch()` POST from the page returns nothing useful.
- Links expire after ~5 minutes and are "not shareable".

So use the user's real browser: Chrome (claude-in-chrome tools) or
whatever browser tool you have access to. **Only download at the user's
direction.** The user asks for the audio and approves the specific files
(filenames, count, approximate size: ~2–3 MB per 30-min file) before
anything is fetched. Don't download in bulk or on speculation.

## 1. Find the feeds for an airport

Navigate to `https://www.liveatc.net/search/?icao=<airport id>`, for
example, `https://www.liveatc.net/search/?icao=klax` (the first load
may show a "Just a moment..." Cloudflare check; wait ~5 s). List feeds
and their archive mounts:

```js
[...document.querySelectorAll('a')].filter(a=>/archive\.php/.test(a.href))
  .map(a=>a.textContent.trim()+' | '+a.href.split('m=')[1]).join('\n')
```

Then open any `https://www.liveatc.net/archive.php?m=<mount>` page and map feed names
to the **facility values** used in archive filenames (the `<select name=facility>` lists
every feed site-wide, so filter):

```js
[...document.querySelectorAll('select option')]
  .filter(o=>/KLAX|SOCAL Approach/i.test(o.textContent))
  .map(o=>o.textContent.trim()+' = '+o.value).join('\n')
```

Example (KLAX): `KLAX-Twr-South`, `KLAX-Gnd`, `KLAX3N-Del`, `KLAX-Dep-App`,
`KLAX-East-Feeder-KPOC`. Some mounts include the frequency in the name
(`<ICAO>1-…-Twr1-118.700`). Some feeds have gaps — if a slot is "File Not Found", try
the airport's other (`#2`/`3N`-style) feed for the same position.

Filenames are `<facility>-<Mon>-<DD>-<YYYY>-<HHMM>Z.mp3`, where HHMM is the UTC slot
start (`0000Z`, `0030Z`, …). A slot's file is published only **after the slot ends**
(the 1730Z file appears a little after 18:00Z).

Pick slots from the event times (e.g. ADS-B takeoff/landing times),
and include the approach/departure frequency if appropriate, not just
tower — the check-in with approach often has the clearest statement of
intentions.

## 2. Get a signed link via the archive form

On an `archive.php?m=...` page (any mount works; the facility select has all feeds),
define a helper, submit, and follow the signed link in the same tab:

```js
window.getArchive = async (fac, date, time) => {   // date 'YYYYMMDD', time 'HHMMZ'
  const f = document.forms[4];                       // the archive-response.php form
  f.elements[0].value = date.slice(0,4)+'-'+date.slice(4,6)+'-'+date.slice(6);
  f.date.value = date; f.facility.value = fac; f.time.value = time;
  if (f.facility.value !== fac) return 'bad facility';
  [...f.elements].find(e => e.type === 'submit').click();
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 500));
    const a = [...document.querySelectorAll('a')]
      .find(a => a.textContent.includes(fac+'-') && /\.mp3/.test(a.textContent));
    if (a) { setTimeout(() => { location.href = a.href }, 100); return 'found ' + a.textContent.trim(); }
    if (/File Not Found/.test(document.body.innerText)) return 'not found';
  }
  return 'timeout';
};
await getArchive('KLAX-Twr-South', '20261008', '1730Z')
```

Notes:
- `document.forms[4]` was the archive form at the time; if that changes, find the form
  whose `action` is `archive-response.php`.
- Wait ~2 s after page load before calling it (Turnstile must show "Success!").
- If the call errors with "Inspected target navigated", Cloudflare reloaded the page;
  just re-define the helper and call it again.
- Don't return or print the signed URL — the browser tool redacts query strings anyway.

## 3. Save the mp3

The tab is now on `archive.liveatc.net/...mp3?...` (same origin as the file), so a
same-origin fetch works and can be saved via a blob download (the cross-origin fetch
from www.liveatc.net fails CORS, and `<a download>` is ignored cross-origin):

```js
await new Promise(r => setTimeout(r, 1500));
const r = await fetch(location.href); const b = await r.blob();
if (b.size > 100000) {
  const d = document.createElement('a');
  d.href = URL.createObjectURL(b);
  d.download = decodeURIComponent(location.pathname.split('/').pop());
  document.body.appendChild(d); d.click();
}
location.pathname + ' ' + r.status + ' ' + b.size
```

The file lands in the downloads directory. Move it into the project
directory (not the session scratchpad) and close the browser tab when
done. Repeat steps 2–3 per file (navigate back to an `archive.php`
page each time).

The downloaded mp3s can have damaged frames ("Header missing" from ffmpeg). That's
harmless for full-file transcription, but always **re-encode** when cutting clips
(`-ar 16000 -ac 1`), because stream-copied cuts can come out near-silent.
