-- Deactivate deprecated shared-sqft bucket rows across all months.
-- Historical stg_warehouse_allocation data stays intact for reconciliation.
UPDATE alloc_warehouse_shared_sqft_monthly
SET is_active  = FALSE,
    updated_at = NOW()
WHERE program_bucket IN (
    'Demo - ADV - Inbound',
    'OGP - ADV - Inbound',
    'Demo - ADV - Outbound',
    'OGP - ADV - Outbound'
)
AND is_active = TRUE;
