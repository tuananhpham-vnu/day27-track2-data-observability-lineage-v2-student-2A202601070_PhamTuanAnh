-- Daily completed-order revenue for the CEO dashboard.
--
-- FAN-OUT GUARD: the original version joined straight onto stg_customers where
-- is_active = true. An SCD-2 dimension can legitimately carry more than one
-- active row per customer (a late-arriving update that never closed the previous
-- version), and a left join against it silently duplicates order rows. The sum
-- then inflates revenue with no SQL error and no failing pipeline - the CEO sees
-- a number that is simply wrong.
--
-- The fix is to collapse the dimension to exactly one row per customer *before*
-- joining. `unit_tests.yml` pins this behaviour with a two-active-version
-- fixture, and `tests/assert_revenue_matches_orders.sql` re-checks it against
-- real data on every build.

with completed_orders as (
    select *
    from {{ ref('stg_orders') }}
    where status = 'completed'
),

active_customers as (
    select *
    from {{ ref('stg_customers') }}
    where is_active = true
),

-- Exactly one row per customer: the most recent version wins.
current_customers as (
    select *
    from (
        select
            *,
            row_number() over (
                partition by customer_id
                order by valid_from desc nulls last
            ) as version_rank
        from active_customers
    )
    where version_rank = 1
)

select
    o.order_date,
    count(*) as completed_order_rows,
    sum(o.amount_usd) as daily_revenue
from completed_orders o
left join current_customers c
    on o.customer_id = c.customer_id
group by 1
order by 1
