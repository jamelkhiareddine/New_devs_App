# Revenue dashboard investigation

## Summary

The reported figures and data exposure are reproducible from the dashboard revenue path. The defects are independent:

1. Redis cache entries are shared between tenants.
2. The endpoint marketed as a monthly summary aggregates every reservation for the property.
3. Date boundaries are calculated as naive UTC dates instead of local dates for the property's time zone.
4. Exact database decimals are converted to binary floating-point values before being sent to the UI.

The required fixes are in `backend/app/services/cache.py`, `backend/app/services/reservations.py`, `backend/app/api/v1/dashboard.py`, and `frontend/src/components/RevenueSummary.tsx`.

## 1. Cross-tenant revenue disclosure (critical)

### Cause

`get_revenue_summary()` builds its Redis key using only the property ID:

```python
cache_key = f"revenue:{property_id}"
```

Property IDs are not globally unique: the seed data assigns `prop-001` to both `tenant-a` (Sunset Properties) and `tenant-b` (Ocean Rentals). Therefore, after Sunset requests `prop-001`, Ocean's request for the same ID reads Sunset's cached JSON for five minutes. This exactly explains values changing depending on refresh/request order and is a tenant-data privacy breach.

### Fix

Namespace every cached value by all data-scoping inputs. At minimum:

```python
cache_key = f"revenue:{tenant_id}:{property_id}"
```

For a period-based report, include the reporting period too, for example:

```python
cache_key = f"revenue:{tenant_id}:{property_id}:{year:04d}-{month:02d}"
```

Delete existing `revenue:*` keys during deployment because they may contain incorrectly scoped data. Also ensure cache invalidation uses the identical, tenant-qualified key.

### Verification

1. Authenticate as Sunset and request `prop-001`; record the response.
2. Authenticate as Ocean and request `prop-001`.
3. Confirm the two responses are calculated independently (Ocean must not receive Sunset's `tenant_id`, total, or reservation count), in either request order.

## 2. March dashboard shows lifetime revenue instead of March revenue (high)

### Cause

The dashboard describes its figures as monthly, and `calculate_monthly_revenue()` accepts `month` and `year`. However, the live calculation in `calculate_total_revenue()` has no date predicate:

```sql
WHERE property_id = :property_id AND tenant_id = :tenant_id
```

It consequently sums all reservations for that property, not those belonging to the selected month. The existing `calculate_monthly_revenue()` has the intended date predicate in a commented simulation but always returns `Decimal('0')`, so it cannot provide the report either.

### Fix

Make the dashboard API explicitly accept a validated `year` and `month` (or an explicit start/end period), and pass them to the revenue service. Use one database query that includes the tenant, property, and half-open time range:

```sql
WHERE r.property_id = :property_id
  AND r.tenant_id = :tenant_id
  AND r.check_in_date >= :period_start_utc
  AND r.check_in_date < :period_end_utc
```

Use `COALESCE(SUM(r.total_amount), 0)` so an empty period has an exact zero and preserve `COUNT(*)` from the same filtered query. Do not rely on the UI label to imply a date range—the period must be an API input and cache-key component.

## 3. Incorrect reporting month at time-zone boundaries (high)

### Cause

`calculate_monthly_revenue()` forms naive datetimes such as `datetime(2024, 3, 1)` and compares them directly to `TIMESTAMP WITH TIME ZONE` values. That treats a report period as a UTC calendar month (or depends on the database session time zone), not as the property's local calendar month.

The seed data deliberately demonstrates this: `res-tz-1` starts at `2024-02-29 23:30:00+00`. For `prop-001`, whose time zone is `Europe/Paris`, this is `2024-03-01 00:30` locally and belongs in March. A UTC-month boundary would incorrectly exclude it from the March report.

### Fix

Read the property's time zone only after constraining it by both `property_id` and `tenant_id`. Construct local midnight at the beginning of the requested month and at the beginning of the next month with `zoneinfo.ZoneInfo(property_timezone)`, then convert both instants to UTC before binding them to SQL. Keep the `[start, end)` comparison shown above.

Illustrative Python:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

zone = ZoneInfo(property_timezone)
local_start = datetime(year, month, 1, tzinfo=zone)
local_end = datetime(next_year, next_month, 1, tzinfo=zone)
period_start_utc = local_start.astimezone(timezone.utc)
period_end_utc = local_end.astimezone(timezone.utc)
```

With the supplied data, March 2024 for Sunset's `prop-001` should include `res-tz-1` ($1,250.000) and the three decimal test reservations ($1,000.000), for an exact total of **$2,250.000** and four reservations.

## 4. Rounding and cent discrepancies (high)

### Cause

The database correctly uses `NUMERIC(10, 3)` and the service initially converts the aggregate to `Decimal`. The endpoint then discards that precision:

```python
total_revenue_float = float(revenue_data['total'])
```

The frontend receives a JavaScript `number` and rounds it using `Math.round(data.total_revenue * 100) / 100`. Binary floating point cannot represent many decimal fractions exactly, and rounding an aggregate only in the presentation layer can differ from the finance system's currency rounding policy. The provided `333.333`, `333.333`, and `333.334` records are specifically useful for exposing this issue.

### Fix

Keep amounts as decimal strings (or integer minor units) throughout the API boundary. A practical compatible response is:

```python
"total_revenue": format(total_revenue.quantize(Decimal("0.01")), "f")
```

Use an explicitly agreed policy for sub-cent values—normally aggregate in `NUMERIC`, then round once using `ROUND_HALF_UP` to the currency's minor-unit precision. On the client, treat `total_revenue` as a string and format it with a decimal/currency library or render the server-rounded amount. Do not use `float`, `number`, or `Math.round` for accounting values.

## Additional safeguards

- Return a 404 (or an empty, non-cacheable result) when the tenant-qualified property lookup does not exist; never serve a cached object whose embedded `tenant_id` differs from the authenticated tenant.
- Derive `tenant_id` exclusively from `current_user.tenant_id`; do not accept tenant identity from client query parameters or headers.
- Add integration tests for cache isolation in both request orders, duplicate property IDs across tenants, Paris/New York month boundaries, empty months, and decimal totals.
- Add a regression test asserting the API response amount remains a string/integer minor-unit value rather than a float.
- Log cache keys and tenant IDs in secure server-side audit logs without logging monetary payloads or credentials.

## Recommended deployment order

1. Deploy the tenant-qualified cache-key fix and clear the legacy revenue cache keys immediately.
2. Deploy the period/time-zone query and decimal API contract together, then invalidate the new period-scoped entries.
3. Run the integration tests with the supplied Sunset and Ocean accounts before re-enabling the dashboard for clients.
