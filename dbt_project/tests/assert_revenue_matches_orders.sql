-- SINGULAR BUSINESS TEST: the mart must not invent revenue.
--
-- fct_daily_revenue is an aggregate of stg_orders and nothing else. If the join
-- onto the customer dimension ever fans out, the daily total and the row count
-- both rise while stg_orders is unchanged. `unique(order_date)` cannot see this
-- (the GROUP BY still yields one row per day) and `not_null` cannot either -
-- the pipeline reports SUCCESS while the CEO dashboard is simply wrong.
--
-- Passes when the query returns zero rows.

with source_truth as (
    select
        order_date,
        count(*) as expected_rows,
        sum(amount_usd) as expected_revenue
    from {{ ref('stg_orders') }}
    where status = 'completed'
    group by 1
),

mart as (
    select order_date, completed_order_rows, daily_revenue
    from {{ ref('fct_daily_revenue') }}
)

select
    m.order_date,
    m.completed_order_rows,
    s.expected_rows,
    m.daily_revenue,
    s.expected_revenue
from mart m
join source_truth s using (order_date)
where m.completed_order_rows != s.expected_rows
   -- tolerance absorbs float noise only, not a duplicated order
   or abs(m.daily_revenue - s.expected_revenue) > 0.01
