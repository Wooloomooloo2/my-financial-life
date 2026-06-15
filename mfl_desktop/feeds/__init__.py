"""Bank-feed providers (ADR-077, Arc H).

A *provider* pulls transactions from an institution and hands them to the
existing import pipeline as ordinary raw-txn dicts (so staging, FITID/hash
dedup, the manual-match heuristic, review, and commit are all reused). The
first provider is GoCardless Bank Account Data (free UK/EU Open Banking);
OFX Direct Connect / SimpleFIN (US) are later rounds with the same surface.

Everything here is Qt-free and stdlib-only (no new dependency).
"""
