-- ADR-130 Phase 3: record the bank's posting date separately from the user's
-- spend date.
--
-- The user enters a transaction on the day they SPEND; the bank posts it a day
-- or three later. Keeping both lets the register show the spend date while
-- reconciliation ranges on the bank date (COALESCE(bank_posted_date,
-- posted_date)) — which is what stops the boundary/date-scramble we saw when a
-- 23-June bank line was entered as 22 June. Set on import (from the OFX/CSV
-- posting date) for new-from-bank rows and when a download matches a hand-
-- entered row; NULL for anything not yet seen in a download.

ALTER TABLE txn ADD COLUMN bank_posted_date TEXT;
