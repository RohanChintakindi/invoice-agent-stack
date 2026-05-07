"""Cash reconciliation: match incoming wires to open invoices.

Pipeline:
    wire arrives
        -> entity resolution (memo string -> payer_id)
        -> candidate generation (open invoices for that payer, amount/date filter)
        -> feature scoring (rapidfuzz, amount/date deltas)
        -> XGBoost ranker + isotonic calibration -> per-pair probability
        -> if no high-prob single match: subset-sum bundler
        -> trust-aware threshold from TrustEngine -> auto-post OR queue for review

Trust integration: AUTO_MATCHED bumps trust on confirm, HUMAN_OVERRIDE
penalises trust when a reviewer reverses an auto-match.
"""
