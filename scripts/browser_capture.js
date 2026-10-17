// Reproducible live capture for `signalpost ingest`, for machines that cannot reach
// data.brreg.no directly. Open https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html
// in a browser, paste this into the DevTools console, set ORGNR, and run. It prints the
// SHA-256 and copies the capture JSON to the clipboard; save it as data/live_captures/<orgnr>.json,
// then: python run.py ingest data/live_captures/<orgnr>.json <orgnr> --sha256 <hash>
// NOTE: roles contain birth dates; captures are git-ignored and Signalpost never stores birth dates.
const ORGNR = '923609016';
const canon = v => Array.isArray(v) ? '[' + v.map(canon).join(',') + ']'
  : (v && typeof v === 'object') ? '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}'
  : JSON.stringify(v);
const urls = [`https://data.brreg.no/enhetsregisteret/api/enheter/${ORGNR}`,
              `https://data.brreg.no/enhetsregisteret/api/enheter/${ORGNR}/roller`,
              `https://data.brreg.no/regnskapsregisteret/regnskap/${ORGNR}`];
const responses = [];
for (const u of urls) {
  const r = await fetch(u, {headers: {Accept: 'application/json'}, cache: 'no-store'});
  const t = await r.text();
  responses.push({url: u, status: r.status, retrieved_at: new Date().toISOString().replace(/\.\d+Z$/, '+00:00'),
                  last_modified: r.headers.get('last-modified'), body: t ? JSON.parse(t) : null});
}
const text = canon({captured_with: 'browser fetch (scripts/browser_capture.js)', responses});
const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)))]
  .map(b => b.toString(16).padStart(2, '0')).join('');
copy(text); console.log('sha256', hash, 'statuses', responses.map(r => r.status));
