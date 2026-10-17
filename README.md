# Signalpost

Evidence-backed company intelligence for Norwegian organisation numbers. Built for the Builderr.ai hackathon "Build An Agent That Finds Company Information".

Give Signalpost a 9-digit Norwegian organisation number. It returns verified facts from the official Brønnøysund registers. Every fact carries the source URL that states it, the JSON location of the value, and the retrieval time. Signalpost also stores each snapshot so later runs report what changed.

> **Status (honest):** the code, tests and offline benchmark are complete and passing. The live dataset statistics and live benchmark below are **not yet filled in**. The development sandbox could not reach `data.brreg.no` because its network allowlist refused the connection. Run `python run.py daily` on a networked machine to produce them; the [Results](#8-tests-and-benchmark-results) section says exactly what to paste where.

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

Endpoints and field names come from Brreg's [API documentation](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html) and the official [regnskapsregister-api OpenAPI spec](https://github.com/brreg/regnskapsregister-api). Brreg describes the accounts API as a temporary R&D API that can change without notice. Signalpost therefore validates its output strictly, and a circuit breaker stops calling it after 5 consecutive failures in a run.

## 4. Setup

```bash
git clone <REPO_URL> signalpost && cd signalpost
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt                        # requests, pydantic, pytest
```

The tool requires Python ≥ 3.10 and was tested on 3.13. Alternatively, run `pip install -e ".[dev]"` to get a `signalpost` console command.

## 5. Configuration

Copy `.env.example` to `.env` if you want to change any defaults. Every setting is optional:

| Variable | Default | Meaning |
|---|---|---|
| `SIGNALPOST_DB` | `data/signalpost.sqlite3` | SQLite store |
| `SIGNALPOST_MAX_REQUESTS` | `2000` | Hard outbound-request budget per process run |
| `SIGNALPOST_MAX_SECONDS` | `2700` | Hard run deadline (45 min) |
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
```

`python -m signalpost.cli …` and `signalpost …` work too after `pip install -e .`.

**Exit codes:** `0` ok · `1` dataset target not met · `2` invalid input · `3` not found or removed from the register · `4` identity mismatch (nothing published) · `5` source unavailable · `6` budget exceeded.

**Output** (`--json`) includes `outcome`, `profile.facts[]` with `field`, `value`, `source_url`, `retrieved_via`, `json_path`, `retrieved_at`, `verification` and `status`, plus `related_entities[]`, `unknown_fields[]`, `changes[]`, `explanation`, `warnings[]` and `run` (requests, cache hits, time, cost).

## 7. Dataset

`daily` and `build-dataset` call the official search API using these queries, in order, until the target is reached:
1. `organisasjonsform=ASA` (public limited companies)
2. `organisasjonsform=AS&kommunenummer=0301` (limited companies in Oslo)

Each search page returns full entity records, so 1,000 profiles cost about 10 search requests. Annual-accounts key figures add one request per company. Roles are fetched on single lookups, not during the bulk build, which keeps a daily run under 2,000 requests. Progress is stored per page, so an interrupted build resumes where it stopped. Organisation numbers are checksum-validated and de-duplicated.

The build report and `data/export/dataset_stats.json` include `companies_stored`, `distinct_orgnrs`, `invalid_orgnrs`, `this_run.duplicates`, `missing_core_fields`, `field_coverage`, `facts_without_provenance`, `with_financials`, failures and the stop reason.

**Actual dataset statistics: NOT YET GENERATED.** Paste `data/export/dataset_stats.json` here after the first live run. Do not claim the 1,000-profile requirement until `target_met` is `true`.

## 8. Tests and benchmark results

```bash
python -m pytest -q
```

**Measured in this repository:** 57 tests pass. They cover:
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

**Offline benchmark** (`python run.py --db :memory: benchmark --sample 0`; synthetic fixtures; measured): **12/12** scenario checks passed, evidence completeness **1.0**, explanation grounding **1.0**. Explanation grounding is the share of dates, organisation numbers and amounts in the explanation that are verified fact values.

**Live benchmark: NOT YET RUN.** It needs network access to `data.brreg.no`. `python run.py benchmark --sample 50` reports outcomes, identity accuracy, mean field coverage, evidence completeness, explanation grounding, requests per company, seconds per company and cost, and writes `data/export/benchmark.json`.

## 9. Models and APIs

- **LLM: none.** Fact extraction, identity matching, financial validation and explanations are all deterministic. This was a deliberate choice: the disqualifying failures, wrong company and fabricated figures, are exactly what an unconstrained generator risks.
- **APIs:** the Brreg open APIs listed in section 3. HTTP/JSON, no authentication.

## 10. Expected cost per daily run

| Item | Estimate (not yet measured live) |
|---|---|
| External API cost | **$0.00** (free official open data) |
| Requests, first `daily` run (1,000 companies) | ≈ 10–12 search pages + ≤ 1,000 accounts calls ≈ **1,010–1,020** |
| Requests, later `daily` runs | ≈ 10 batch-refresh requests + fallbacks for missing records ≈ **10–100** |
| Single `company` lookup | 3 requests (entity, roles, accounts), or 1 with `--no-enrich` |
| Time | Under 10 minutes at 0.1 s spacing plus API latency (estimate) |

The hard limits cannot be exceeded. The client refuses request 2,001 and any request after 45 minutes, and the run then finishes and exports what it has. Retries count toward the budget.

## 11. Limitations and known failure modes

- **Not validated live from the build environment.** The schemas come from official docs and the OpenAPI spec. The roles response shape and the batch filter `organisasjonsnummer=` on search have not been seen live. The parsers are defensive, and the identity gate drops anything not requested. If the batch filter is ignored, refresh falls back to individual lookups, which costs about 1 request per company and stays within budget.
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

1. `python run.py daily`, then `python run.py benchmark --sample 50`
2. Paste `data/export/dataset_stats.json` and `benchmark.json` figures into sections 7, 8 and 10.
3. `git add -A && git commit -m "Add live dataset and benchmark"` (commit `data/export/`, not the SQLite file)
4. Push to a public repository and submit the URL, the output of `git rev-parse HEAD`, the command `python run.py daily`, "no LLM; Brreg open APIs", and "$0 external cost".
