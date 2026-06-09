-- 0016_security_price_fetch_status.sql — give-up memory for uncovered tickers (ADR-049).
--
-- Records when a Tiingo *history* fetch for a security came back "not covered"
-- (HTTP 404 / unknown ticker, or a successful-but-empty series). The launch
-- auto-backfill (`securities_missing_history`) and the latest-refresh
-- (`securities_to_price`) skip such a security while this timestamp is within a
-- cooldown window (default 30 days), so an uncovered ticker is no longer
-- re-fetched on EVERY launch — the open item ADR-047 flagged.
--
-- It is a COOLDOWN, not a tombstone: after the window passes the security is
-- retried automatically, so a ticker Tiingo starts covering (or one the owner
-- later corrects) heals itself. A successful fetch clears the column; the
-- per-security Stock Record "Fetch from Tiingo" button ignores the cooldown
-- (explicit user override) and sets/clears the column on its own result.
--
-- Plain ALTER TABLE ADD COLUMN (nullable) — no table rebuild needed, since
-- we're adding a column, not changing a CHECK. Existing rows get NULL
-- (= "never failed, eligible to fetch"), which is exactly right.

PRAGMA foreign_keys = ON;

ALTER TABLE security ADD COLUMN price_fetch_failed_at TEXT;  -- ISO datetime; NULL = ok/untried
