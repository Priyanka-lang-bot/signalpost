# Signalpost architecture

## Trust boundary

The only data Signalpost publishes is data that is read back from an authoritative payload of the **requested** entity. Each design rule below exists to keep that boundary intact.

| Rule | Where | Why |
|---|---|---|
| The organisation number is the only identity key | `entity_resolution.verify_identity` | Similar names never match, merge or disambiguate. |
| Payload number **and** canonical URL must equal the request | same | A mis-routed or cached response for another entity is rejected (`identity_mismatch`, exit 4). |
| Accounts items are filtered by `virksomhet.organisasjonsnummer` | `entity_resolution.accounts_items_for` | Figures for another company are discarded. |
| Only `SELSKAP` accounts with currency, a period end and finite numbers | `fact_extraction.extract_financials` | Group figures are never shown as company figures, and nothing is coerced from strings. |
| Each fact carries a `json_path` that is re-read before publishing | `evidence.check_fact` | The value is in the source, at that location, with the same type. |
| Explanations are templates over verified facts | `explain.py` | No generated value can appear. |
| Related entities are references, not merges | `RelatedEntity` | Parent units, auditors and accountants stay separate records. |

## Update semantics

- Facts are stored per `(orgnr, field)`. Every add, change or removal is appended to `fact_history` together with its source URL and retrieval time.
- `freshness.decide` returns `new`, `unchanged`, `changed` or `stale`. A stale observation is one with an older `Last-Modified` or retrieval time than the stored fact. Stale observations never overwrite.
- A stored fact is removed only when its group (`entity`, `roles` or `financials`) was actually fetched in this run and no longer states it. A failed or skipped enrichment therefore never deletes data.
- HTTP 410 (removed for legal reasons) purges the entity and logs the purge.

## Budget enforcement

`BudgetedClient` counts every attempt, retries included, and raises `BudgetExceeded` *before* sending request number `max_requests + 1` or any request after `max_seconds`. Callers catch it and finish gracefully: the dataset build records its stop reason, and enrichment adds a warning. Cache hits do not count. A circuit breaker disables the accounts API after 5 consecutive failures in a run.

## Adding a source

Implement a `RegistrySource` with `lookup(orgnr) -> LookupResult` that returns `SourceRecord`s carrying `canonical_url`, `retrieved_via` and `retrieved_at`. Add a mapping in `fact_extraction` that records `json_path`s, plus a fact group in `freshness.fact_group`. The identity gate and evidence checks then apply unchanged.
