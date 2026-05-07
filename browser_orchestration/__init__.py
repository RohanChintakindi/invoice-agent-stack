"""Browser orchestration vertical.

Production layer on top of browser-use/browser-harness:
  - Job queue (SQLite-backed; ARQ+Redis swap-in for prod)
  - Fernet credential vault
  - Silent-failure validator (Claude vision + pydantic schema)
  - Trust-aware schedule policy
  - Per-portal observability metrics

Reads payer trust score to gate scrape frequency. Writes
SILENT_FAIL_CAUGHT / CLEAN_EXTRACTION_STREAK back to the trust engine.
"""
