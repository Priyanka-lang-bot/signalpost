# Signalpost

Evidence-backed company intelligence for Norwegian organisation numbers. Built for the Builderr.ai hackathon "Build An Agent That Finds Company Information".

Give Signalpost a 9-digit Norwegian organisation number. It returns verified facts from the official Brønnøysund registers. Every fact carries the source URL that states it, the JSON location of the value, and the retrieval time. Signalpost also stores each snapshot so later runs report what changed.

> **Status (honest, measured 2026-10-17):**
> - ✅ The API was verified against the official OpenAPI spec (*Open Data from the CCR*, v2.0.0) and with live requests.
> - ✅ **Dataset:**
>   - 1,000 exported profiles, 1,000 distinct organisation numbers, 0 invalid
>   - 36,475 facts, 0 without provenance, `target_met: true`
>   - roles for **1,000/1,000** companies
>   - financials for **976/1,000**; the other 24 are explicitly marked unavailable, never filled in
> - ✅ Offline store audit: **36,475/36,475** stored facts re-checked against the cached raw API responses, **0 failures**. The checks cover value, source URL, identity, timestamp, financial rules and related-entity consistency.
> - ✅ Second `daily` run: **1,047 outbound requests, 401.5 s, $0.00 measured**. That is within the 2,000-request, 45-minute and $10 limits.
> - ✅ Live benchmark: **50/50** lookups `ok`, identity accuracy **1.0**, evidence completeness **1.0**, **150** requests, **35.03 s**, **$0.00** (section 8).
> - ✅ Tests: `python -m pytest -q` → **91 passed, 1 skipped** (section 8).

---

## 1. Problem and solution

**Problem.** Given an organisation number, find correct, current facts about *that exact* company. Never mix in a similarly named company, and never invent a value. Two mistakes are especially costly here: a wrong-company publication and a fabricated financial figure.

**Solution.** A deterministic pipeline that uses only official structured registry data. Its guiding rule is that nothing is published unless it can be re-read from the source payload of the requested entity.

```
input ─▶ validate (mod-11) ─▶ fetch authoritative record ─▶ identity gate ─▶ extract facts
      ─▶ evidence check (re-read each value at its JSON path) ─▶ compare with SQLite store
      ─▶ save changes + audit history ─▶ template explanation ─▶ JSON / text
```

## 2. Architecture

| Module | Role |
|---|---|
| `orgnr.py` | Normalises input (`"NO 923 609 016 MVA"` → `923609016`) and enforces 9 digits, a leading 8 or 9, and the modulus-11 check digit. |
| `http.py` | `BudgetedClient`. Every outbound call goes through it. It enforces a **hard** request budget and run deadline, plus retries with exponential backoff (honours `Retry-After`), rate spacing and a SQLite cache. |
| `sources/brreg.py` | Adapter for the Enhetsregisteret API (entities, sub-entities, roles, search) and the Regnskapsregisteret key-figures API. New sources implement the small `sources/base.py` contract. |
| `entity_resolution.py` | Identity gate. The payload's `organisasjonsnummer` **and** the record's canonical URL must both equal the requested number. Accounts items are filtered by `virksomhet.organisasjonsnummer`. Names are never used for matching. |
| `fact_extraction.py` | Deterministic field mapping. Each fact records its `json_path`. Financial figures are published only from identity-matched **company (SELSKAP)** accounts with an ISO currency and a period end, and only when the value is a finite number. |
| `evidence.py` | Re-reads each fact's `json_path` in the fetched payload and requires an exact value and type match. Roles are re-derived from the payload. Failing facts are dropped. |
| `storage.py` / `freshness.py` | SQLite holds current facts, an append-only `fact_history`, the HTTP cache and resumable dataset progress. Older observations never overwrite newer ones. A fact is removed only if its group was actually re-fetched. A 410 response purges the stored copy, as Brreg asks. |
| `explain.py` | Plain-language summary built from a template over verified facts only. **No LLM** is used, so it cannot add a value. |
| `dataset.py` | Resumable dataset build (official search API, pages of 100), batch refresh with an identity gate on every returned item, export and statistics. |
| `benchmark.py` | Offline correctness suite on labelled synthetic fixtures, plus a live sample benchmark. |

Related entities are referenced by number and **never merged**. These include a branch's parent, and auditor or accountant firms in roles. See [`docs/architecture.md`](docs/architecture.md).

## 3. Public sources

All sources are from Brønnøysundregistrene, open data under the [NLOD 2.0 licence](https://data.norge.no/nlod/en/2.0). No API key is needed and there is no charge.

| Source | Endpoint | Used for |
|---|---|---|
| Enhetsregisteret | `GET https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}` | Name, legal form, addresses, NACE industry, employees, dates, VAT and business-register flags, bankruptcy and liquidation flags, parent |
| Enhetsregisteret | `GET …/underenheter/{orgnr}` | Sub-entities (branches). Kept distinct from their parent. |
| Enhetsregisteret | `GET …/enheter/{orgnr}/roller` | CEO, board, auditor, accountant. Birth dates are deliberately **not** stored. |
| Enhetsregisteret | `GET …/enheter?page=&size=&…` | Dataset build (limit: `(page+1)*size ≤ 10 000`) and batch refresh |
| Regnskapsregisteret | `GET https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}` | Key figures from the latest approved annual accounts |

Endpoints, parameters and field names were checked against the official OpenAPI spec, `https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/openapi.json` (title *Documentation for Open Data from the Central Coordinating Register for Legal Entities (CCR)*, version 2.0.0, server `https://data.brreg.no`, `security: []`). The accounts endpoint was checked against the official [regnskapsregister-api spec](https://github.com/brreg/regnskapsregister-api). All endpoints were then exercised live on 2026-10-17.

**Verified facts about the API:**

| Topic | Spec (v2.0.0) | Observed live |
|---|---|---|
| Single entity | `GET /enhetsregisteret/api/enheter/{enhetorgnr}` → 200 `Enhet` or `SlettetEnhet` (discriminator `respons_klasse`), 400 `BadRequest`, 404, 410 `GoneEnhet`, 500 | 200 for 923609016; 404 for a well-formed unknown number; 400 with a `valideringsfeil` body for `12345` |
| Sub-entity | `GET …/underenheter/{underenhetorgnr}`, same status set; address field `beliggenhetsadresse` | Equinor has 35 sub-entities; fields as specified |
| Search | `GET …/enheter` with filters such as `organisasjonsform`, `kommunenummer`, `naeringskode`, `organisasjonsnummer` (comma list), plus `sort`, `page`, `size`. Response `Enheter` = `_embedded.enheter[]` + `page{number,size,totalElements,totalPages}` | `organisasjonsnummer=a,b,c` returns only existing matches; `(page+1)*size > 10 000` → HTTP 400, as documented |
| Bulk | `GET …/enheter/lastned` (gzip JSON), `/csv`, `/regneark`; produced nightly around 05:00 with `etag`/`last-modified` | Not used: 11 search requests are enough for 1,000 |
| Updates | `GET …/oppdateringer/enheter` (`dato`, `updatedBefore`, `oppdateringsid`, `organisasjonsnummer`, `includeChanges`, `page`, `size`) | Not used yet; a future refresh optimisation |
| Roles | `GET …/enheter/{orgnr}/roller` → `Roller{rollegrupper[{type,sistEndret,roller[{type,person|enhet,avregistrert,valgtAv?,rekkefolge}]}]}` | Matches; no `fratraadt` field (removed per changelog); `enhet.navn` is an array |
| Auth | `security: []` on open endpoints; `bearerAuth` (Maskinporten) only on `/autorisert-api/*` | No key needed |
| Rate limits | **None documented** | No 429 seen across about 20 live calls. Signalpost still spaces requests and caps them at 2,000 |
| Licence | NLOD 2.0, contact opendata@brreg.no | — |
| Freshness headers | not specified for single lookups | **No `Last-Modified`/`ETag` on `/enheter/{orgnr}`**, so retrieval time is the freshness signal |

**Fields used** (all copied verbatim, with JSON path):
- Organisation number: `organisasjonsnummer`. Name: `navn`.
- Legal form: `organisasjonsform.kode`/`beskrivelse`.
- Addresses: `forretningsadresse`, `postadresse`, and `beliggenhetsadresse` for sub-entities.
- Industry: `naeringskode1..3.kode`/`beskrivelse`.
- Status: `konkurs`, `konkursdato`, `underAvvikling`, `underAvviklingDato`, `underTvangsavviklingEllerTvangsopplosning`, `slettedato`, `respons_klasse`.
- Dates: `registreringsdatoEnhetsregisteret`, `registreringsdatoForetaksregisteret`, `registreringsdatoMerverdiavgiftsregisteret`, `stiftelsesdato`, `vedtektsdato`.
- Employees: `antallAnsatte`, used only when `harRegistrertAntallAnsatte` is not `false`.
- Other: `kapital.*`, `erIKonsern`, `overordnetEnhet`.

The **source URL** attached to each fact is the record's own `_links.self.href`, e.g. `https://data.brreg.no/enhetsregisteret/api/enheter/923609016`. The identity gate requires it to end in the requested number. Live, this held for 1,000/1,000 search results. Brreg describes the accounts API as a temporary R&D API that can change without notice. Signalpost therefore validates its output strictly, and a circuit breaker stops calling it after 5 consecutive failures in a run.

<a id="live-results"></a>
### Live results (2026-10-17)

**Single lookup.** `GET https://data.brreg.no/enhetsregisteret/api/enheter/923609016` returned **HTTP 200**, `organisasjonsnummer` **923609016**, `navn` **EQUINOR ASA**. The number was independently confirmed by Proff, Bloomberg LEI and utdanning.no. The roles and accounts endpoints also returned 200.

The three responses were captured with `scripts/browser_capture.js`; the canonical-JSON SHA-256 is `2f58c0e4…570a0f`. They were then processed by the unmodified pipeline with `python run.py ingest data/live_captures/923609016.json 923609016 --sha256 …`. Results:
- outcome `ok`, 44 facts, **44/44 passed evidence re-check**, `unknown_fields: []`, no warnings
- 14 active roles; one deregistered board member excluded; no birth dates stored
- auditor Ernst & Young AS (976389387) kept as a **separate** related entity
- financials taken from **SELSKAP 2025** (USD), with the three KONSERN filings ignored
- a second run reported "No changes since the previous check"

Fields absent from this record (not applicable): `beliggenhetsadresse`, `slettedato`, `konkursdato`, `underAvviklingDato`, `overordnetEnhet`.

*Transport caveat:* the HTTP calls were made from a browser, because the build sandbox's proxy blocks `data.brreg.no`. On a normal machine, `python run.py company 923609016` makes the same three calls directly.

**Dataset feasibility.** The default queries run live, sorted by organisation number:

| Query | Pages | Total |
|---|---|---|
| ASA | 3 | 213 |
| AS in Oslo (0301) | 8 of 862 | 86,174 |

Totals:
- 11 requests, all HTTP 200, 8.8 s
- 1,013 records received → **1,000 distinct, checksum-valid** organisation numbers; 0 duplicates, 0 invalid
- 1,000/1,000 self-links match
- missing: `naeringskode1` in 2 records; 0 missing for name, legal form, registration date and address
- 14 bankrupt, 13 under liquidation

These records were fetched for verification only. **They are not stored in a Signalpost database yet.**

## 4. Setup

Windows PowerShell (the environment used for the live run):

```powershell
git clone <REPO_URL> signalpost; cd signalpost
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
pip install -r requirements.txt     # requests, pydantic, pytest
python run.py daily                 # the one command: build/resume 1,000 profiles, refresh, enrich, export
                                    # writes data/export/{companies.jsonl,dataset_stats.json,daily_run.json}
```

macOS / Linux:

```bash
git clone <REPO_URL> signalpost && cd signalpost
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py daily
```

Python ≥ 3.10 is required. Tested on CPython 3.13 (Linux) and run live on Windows. No API key, account or `.env` is needed. `pip install -e ".[dev]"` additionally provides a `signalpost` console command.

## 5. Configuration

Copy `.env.example` to `.env` if you want to change any defaults. Every setting is optional:

| Variable | Default | Meaning |
|---|---|---|
| `SIGNALPOST_DB` | `data/signalpost.sqlite3` | SQLite store |
| `SIGNALPOST_MAX_REQUESTS` | `2000` | Hard outbound-request budget per process run |
| `SIGNALPOST_MAX_SECONDS` | `2640` | Hard run deadline (44 min, so an in-flight request of at most `TIMEOUT` seconds and the export still finish within 45 min) |
| `SIGNALPOST_TIMEOUT` / `_MAX_RETRIES` / `_BACKOFF` / `_MIN_INTERVAL` | `20` / `3` / `1.0` / `0.1` | HTTP behaviour |
| `SIGNALPOST_CACHE_TTL` | `3600` | Cache lifetime in seconds. Cache hits are not outbound requests. |
| `SIGNALPOST_ROLES` / `SIGNALPOST_FINANCIALS` | `1` / `1` | Enrichment on single lookups |

There are no secrets and no API keys.

## 6. Running

```bash
# One company (human-readable / JSON)
python run.py company 923609016
python run.py company "NO 923 609 016 MVA" --json --history

# One-command daily run: build/resume the dataset to 1,000, refresh older records, export
python run.py daily

# Individual steps
python run.py build-dataset --target 1000
python run.py refresh
python run.py export            # -> data/export/companies.jsonl, dataset_stats.json
python run.py stats
python run.py benchmark --sample 50

# Process a verified capture of live responses (for machines that cannot reach data.brreg.no)
python run.py ingest data/live_captures/923609016.json 923609016 --sha256 <hash>
```

`python -m signalpost.cli …` and `signalpost …` work too after `pip install -e .`.

**Exit codes:** `0` ok · `1` dataset target not met · `2` invalid input · `3` not found or removed from the register · `4` identity mismatch (nothing published) · `5` source unavailable · `6` budget exceeded.

**Output** (`--json`) includes `outcome`, `profile.facts[]` with `field`, `value`, `source_url`, `retrieved_via`, `json_path`, `retrieved_at`, `verification` and `status`, plus `related_entities[]`, `unknown_fields[]`, `changes[]`, `explanation`, `warnings[]` and `run` (requests, cache hits, time, cost).

## 7. Dataset

`daily` and `build-dataset` call the official search API using these queries, in order, until the target is reached:
1. `organisasjonsform=ASA` (public limited companies)
2. `organisasjonsform=AS&kommunenummer=0301` (limited companies in Oslo)

Each search page returns full entity records, so 1,000 profiles cost about 10 search requests. Annual-accounts key figures add one request per company during the build.

Roles (1 request per company) are not fetched by the build. Instead, the **`enrich`** step (`python run.py enrich`, also the last step of `daily`) backfills roles and retries failed accounts calls. It stops while 25 requests of the run budget remain. It records per company whether the source had data (`found`), had none (`none`, not re-requested) or failed (`failed`, retried on the next run). A daily run therefore converges over runs without exceeding 2,000 requests. Progress is stored per page, so an interrupted build resumes where it stopped. Organisation numbers are checksum-validated and de-duplicated.

The build report and `data/export/dataset_stats.json` include `companies_stored`, `distinct_orgnrs`, `invalid_orgnrs`, `this_run.duplicates`, `missing_core_fields`, `field_coverage`, `facts_without_provenance`, `with_financials`, failures and the stop reason.

### Actual dataset statistics (live, 2026-10-17)

The dataset was produced by two `python run.py daily` runs on a networked Windows machine, using Python's own HTTP client. All figures are measured, read from `data/export/daily_run.json`, `data/export/dataset_stats.json` and the SQLite store.

| Run | What happened | Outbound requests | Retries | Elapsed |
|---|---|---|---|---|
| 1 (12:44–12:51 UTC) | build of 1,000 profiles + accounts | 1,073 | 63 (cause not yet logged by that version) | 424.5 s |
| — | `company 923609016` single lookup | 3 | 0 | — |
| 2 (14:19–14:26 UTC) | refresh 1,000 (10 batch requests, 0 changes) + `enrich`: 999 roles, 24 accounts retries | **1,047** (refresh 10, enrich 1,037) | **27, all HTTP 500** | **401.5 s** |

**Why the run-2 report shows `build.with_roles: 1` but `export.with_roles: 1000`.** The `build` section of `daily_run.json` is a snapshot taken right after the build stage. Roles are not fetched by the build. The `enrich` stage that runs next fetched roles for the other 999 companies, and Equinor's came from the single lookup. `export` is the final state. The report now labels this and gives per-stage request counts.

**Final dataset** (`dataset_stats.json` after run 2):

| Statistic | Value |
|---|---|
| `companies_stored` / `exported_profiles` | **1,000 / 1,000** |
| `distinct_orgnrs` / `invalid_orgnrs` | **1,000 / 0** |
| `facts_total` / `facts_without_provenance` | **36,475 / 0** |
| `target` / `target_met` | 1,000 / **true** |
| Registry state | 1,000 registered main entities (no sub-entities) |
| Missing core fields (name, legal form, NACE code, registration date) | 0 / 0 / 0 / 0 |

**Roles: 1,000 / 1,000.** Every roles fact is sourced from that company's own `…/enheter/{orgnr}/roller` URL, and re-derives exactly from the cached response.
- 998 companies have a CEO (DAGL) or board chair (LEDE).
- 1,196 role-holding entities are linked as separate related entities: 555 auditors, 622 accountants, 19 other.
- No birth dates are stored.

**Financials: 976 / 1,000.** Figures come from the company's latest SELSKAP filing. None of the 3,458 filings received belongs to another organisation number. The other 24 carry an explicit `availability.financials` status and no figures:
- **21: `source_error`.** The accounts API answers **HTTP 500** for these companies, consistently: both of 2 further live calls per company failed. All 21 are banks, credit institutions or insurers (sectors 3200, 3500, 5700, e.g. DNB Bank, Gjensidige Forsikring). The API's own description says figures for banks and insurance companies are not available. They are retried once per daily run and no longer requested after 3 consecutive failed runs.
- **1: `source_error`, fixed next run.** ASP DC ASA (936295592) was never requested in run 2: the outage circuit breaker had paused the accounts API after the bank 500s. Live, it returns 404 (no accounts). Fixed: such companies are now recorded as `not_attempted`, known failures no longer trip the breaker, and they are retried with a single attempt.
- **2: `source_has_none`.** HTTP 404, no accounts in the register.

**Revenue** is present in 769 of the 976. In all 207 gaps, the source filing has no `sumDriftsinntekter`.

**Currency.** NOK 936, USD 28, EUR 11, CAD 1. Each is the `valuta` of the **same** filing as the figures. 6 companies changed reporting currency between years, so the currency is never taken from another year, the share-capital field or a default. Company filings with conflicting duplicates for one period, or malformed currency codes, publish no figures.

**Updates.** Run 2 refreshed all 1,000 entity records and found 0 changes, so `fact_history` holds 36,475 `added` entries and no `changed`/`removed` ones yet. Change, removal and 410 handling are covered by tests on synthetic data (section 8) but have not yet occurred live.

**Export format** `signalpost.companies.v2` (`data/export/companies.jsonl`, 16.9 MB; it was 49.1 MB before compaction). One JSON object per company:
- `facts[]`: `field`, `value`, `source`, `source_url` (the record that states the value), `json_path`, `retrieved_at`, `source_modified_at`, `verification`, `status`, and `retrieval`, a reference into
- `retrievals[]`: the distinct URLs actually fetched for this profile, e.g. a batch-refresh URL listing 100 organisation numbers, stored once instead of on every fact
- `related_entities[]`, `unknown_fields[]`
- `availability`: `roles` / `financials` → `available` | `source_has_none` | `source_error` | `not_attempted` | `not_checked`, with `reason`, `checked_at` and `consecutive_failed_runs`

**Offline store audit** (`python scripts/audit_store.py data/signalpost.sqlite3`, no network). It re-reads each stored fact from the raw response cached when it was fetched. The checks are:
- the value at its JSON path is identical, value and type; roles are re-derived
- the source URL ends with the same organisation number
- the entity record carries that number
- a retrieval timestamp exists
- financial facts come from the company's own latest SELSKAP filing, with an ISO currency
- every role-holding entity is linked as a separate related entity

Result after run 2: **36,475/36,475 audited, 0 failures, `ok: true`**.

After run 1, the related-entity check had found one bug: Equinor's auditor link was erased when the bulk build re-saved the company without roles. That is fixed and covered by a regression test.

No source sent `Last-Modified`, so `source_modified_at` is empty and freshness uses retrieval time.

## 8. Tests and benchmark results

```bash
python -m pytest -q
```

**Automated tests (offline, synthetic fixtures; measured):** `python -m pytest -q` gives **91 passed, 1 skipped** on a checkout without the git-ignored live capture. With the capture present (development machine) it gives 92 passed. They include v2.0.0 schema behaviour (`respons_klasse` deletions, `harRegistrertAntallAnsatte`, roles without birth dates), replay integrity, and a replay of the genuine Equinor capture. That last test is skipped when the git-ignored capture is absent. They cover:
- valid and invalid numbers
- identifier matching
- similar names with different numbers
- branch/parent separation
- mismatched payloads and self-links
- missing and unknown fields
- unsupported and other-company financials
- group-versus-company accounts
- stale snapshots
- changed and removed facts
- 410 purge
- 5xx retry, 429 with `Retry-After`, and malformed JSON
- duplicates and resumable builds
- refresh ignoring unrequested batch results
- hard request and time budgets
- the accounts-API circuit breaker
- `enrich` backfill: reserve respected, entity facts untouched, `none` not re-requested, failures retried
- `target_met` in exported stats
- retry-reason logging
- the store audit detecting a tampered value
- regressions from the data audit: related entities kept when roles are not re-fetched; currency taken from the same filing when it changes between years; newer KONSERN accounts never replacing SELSKAP; conflicting same-period filings publishing nothing; malformed currency codes rejected; another company's newer filing ignored
- the whole daily sequence staying inside one shared request budget
- default limits matching the competition
- `.env.example` loading cleanly
- batch-refresh responses recorded, so refreshed facts stay auditable offline
- final audit: persistent accounts failures stop after 3 runs; known failures use one attempt and do not trip the breaker; breaker skips recorded as `not_attempted`
- export v2 retrieval references and explicit `availability`
- `daily_run.json` written without BOM, with per-stage request counts summing to the run total

The benchmark (`python run.py benchmark --sample 50`, output in `data/export/benchmark.json`, generated 2026-10-17T12:54:54Z) has two clearly separate parts.

**A. Offline scenario checks** use synthetic, labelled fixtures in an in-memory store. They test logic, not live data.

| Metric | Result |
|---|---|
| Scenario checks passed | **12 / 12** |
| Evidence completeness | 1.0 |
| Explanation grounding | 1.0 |

The checks cover identity (A resolves to A, similar-name entity kept separate, branch not merged, mismatched payload rejected), roles and auditor separation, not-found, 410, invalid input, change detection, no false changes, other company's accounts discarded, and non-numeric figures rejected.

**B. Live benchmark** is a random sample (seed 42) of 50 stored companies, looked up again against the real registry with a fresh in-memory store and no cache. Each lookup covers entity, roles and accounts.

| Metric | Result |
|---|---|
| Outcomes | **50 / 50 `ok`** |
| Identity accuracy | **1.0** |
| Mean field coverage | **0.855** |
| Evidence completeness | **1.0** |
| Explanation grounding | **1.0** |
| Outbound requests | **150** (3.0 per company) |
| Elapsed | **35.03 s** (0.701 s per company) |
| Measured external API cost | **$0.00** |

Field coverage is measured over 8 fields: name, legal form, business address, NACE code, registration date, registered employees, roles and revenue. Values below 1.0 come from fields the source does not state for every company, chiefly registered employees and revenue. Missing values are reported as unknown and never filled in.

Explanation grounding is the share of dates, organisation numbers and amounts in the explanation that are verified fact values.

*Neither part is a competition score.*

## 9. Models and APIs

- **LLM: none.** Fact extraction, identity matching, financial validation and explanations are all deterministic. This was a deliberate choice: the disqualifying failures, wrong company and fabricated figures, are exactly what an unconstrained generator risks.
- **APIs:** the Brreg open APIs listed in section 3. HTTP/JSON, no authentication.

## 10. Expected cost per daily run

| Item | Measured / estimated |
|---|---|
| External API cost | **$0.00 measured.** No paid API is called; the only host in the code is `data.brreg.no`, open data. Declared cost per request is $0, so the declared total is also $0.00 |
| Run 1 of `daily` (build of 1,000 + accounts) | **1,073 requests, 424.5 s measured** (incl. 63 retries) |
| Live benchmark, 50 companies | **150 requests, 35.03 s measured** |
| Run 2 of `daily` (refresh 1,000 + `enrich` of 999 roles and 24 accounts) | **1,047 requests (10 + 1,037), 401.5 s measured**, 27 retries (all HTTP 500 from the accounts API for banks/insurers) |
| Later `daily` runs | ≈ 10 batch-refresh requests + ≤ 22 single-attempt accounts retries, falling to ≈ 10 after 3 runs (**estimate**) |
| Single `company` lookup | 3 requests (entity, roles, accounts), or 1 with `--no-enrich` |

**Limits (enforced in code, per `daily` process):**
- **2,000 requests.** `BudgetedClient` counts every attempt, retries included. It refuses the next request once 2,000 is reached, and `enrich` stops while 25 remain. Build, refresh and enrich share one client, which is tested.
- **45 minutes.** No request starts after 44 min. A request in flight can take at most its 20 s timeout, and export makes no requests.
- **$10.** No paid API is called: the only host in the code is `data.brreg.no`. Declared cost is $0.00 per request, and $0.00 was measured.

The first build's 424.5 s and 1,073 requests are well inside these limits.

## 11. Limitations and known failure modes

- **No accounts figures for banks, credit institutions and insurers:** the accounts API returns HTTP 500 for them, as its documentation anticipates. 21 of the 1,000 companies are affected and are marked `source_error`.
- **Roles need two runs on a fresh install:** the build skips roles to stay within budget, and `enrich` adds them. On a fresh install, run 1 should backfill most roles with its leftover budget, about 900 (an **estimate**: 2,000 − 25 reserve − ≈1,073 for the build). Run 2 completes them.
- The cause of run 1's 63 retries was not logged; `run.retry_reasons` records it from run 2 on (27 × HTTP 500).
- Update detection and 410 removal are verified by tests on synthetic payloads. On live data, the first build recorded only `added` history entries. Change detection on live data is exercised by running `python run.py refresh` on a later day, and no removed (410) company has been observed live yet.
- The default (unsorted) search order is undocumented, so the builder always sends `sort=organisasjonsnummer,ASC` to make resumed paging stable.
- The accounts API is temporary, covers ordinary accounting regimes only (no banks or insurers), and gives no group figures. Signalpost publishes company figures only.
- Brreg data has no per-field modification dates. Freshness uses the HTTP `Last-Modified` header when present, otherwise retrieval time.
- The dataset is a reproducible slice of the register (ASA, then Oslo AS), not a random sample.
- Phone and e-mail contact fields are intentionally not collected.

## 12. Privacy, provenance and responsible access

- Only official open data under NLOD 2.0 is used. There is no scraping.
- Personal data is minimised. Role holders' names and roles are kept because they are public register information. Birth dates are not stored, nor are personal contact details.
- Entities removed for legal reasons (HTTP 410) are purged from the store, as Brreg requests.
- Every published value can be traced to a source URL, JSON path and timestamp. Unverifiable values are dropped, not guessed. Unknown values are listed in `unknown_fields`.
- Test fixtures are **synthetic and labelled** (`SYNTETISK TESTSELSKAP`). They are used only with in-memory databases and never enter the production store or exports.

## 13. Submission

- **Run command:** `python run.py daily`
- **Model:** no LLM
- **APIs:** Brønnøysund open data, no key
- **Cost:** $0.00 measured; limits enforced in code (2,000 requests, 45 min)
- **Dataset:** `data/export/companies.jsonl` and `data/export/dataset_stats.json`, committed. The SQLite database and raw captures are not committed.
- **Repository URL and commit:** the public repository URL and the output of `git rev-parse HEAD` after the final push.
