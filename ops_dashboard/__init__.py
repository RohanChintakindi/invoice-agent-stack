"""Unified ops dashboard.

Two pieces:
  - ops_dashboard/api.py — FastAPI JSON service that reads the shared
    SQLite DB across all 3 verticals (voice / browser / cash_recon).
  - ops_dashboard/web/ — Next.js 14 frontend that renders payer
    timelines, trust evolution, and drill-downs into each vertical.
"""
