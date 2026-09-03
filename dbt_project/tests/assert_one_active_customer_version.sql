-- SINGULAR BUSINESS TEST: the SCD-2 customer dimension must have at most one
-- active version per customer.
--
-- This is the upstream cause of revenue fan-out. Catching it here names the
-- broken dataset directly instead of leaving the team to reverse-engineer an
-- inflated total from the mart.
--
-- Passes when the query returns zero rows.

select
    customer_id,
    count(*) as active_versions
from {{ ref('stg_customers') }}
where is_active = true
group by 1
having count(*) > 1
