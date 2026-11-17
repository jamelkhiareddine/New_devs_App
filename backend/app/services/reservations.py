from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict
from zoneinfo import ZoneInfo

from sqlalchemy import text


async def _get_database_pool():
    """Return the shared, initialized application database pool."""
    from app.core.database_pool import db_pool

    if not db_pool.session_factory:
        await db_pool.initialize()
    if not db_pool.session_factory:
        raise RuntimeError("Revenue database is unavailable")
    return db_pool


async def calculate_monthly_revenue_summary(
    property_id: str, tenant_id: str, year: int, month: int
) -> Dict[str, Any]:
    """Calculate one tenant property's revenue for its local calendar month."""
    db_pool = await _get_database_pool()

    async with db_pool.get_session() as session:
        property_result = await session.execute(
            text("""
                SELECT timezone
                FROM properties
                WHERE id = :property_id AND tenant_id = :tenant_id
            """),
            {"property_id": property_id, "tenant_id": tenant_id},
        )
        property_row = property_result.fetchone()
        if not property_row:
            raise LookupError("Property not found for the authenticated tenant")

        try:
            property_timezone = ZoneInfo(property_row.timezone)
        except Exception as error:
            raise ValueError("Property has an invalid timezone") from error

        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        period_start = datetime(year, month, 1, tzinfo=property_timezone).astimezone(timezone.utc)
        period_end = datetime(next_year, next_month, 1, tzinfo=property_timezone).astimezone(timezone.utc)

        revenue_result = await session.execute(
            text("""
                SELECT currency, SUM(total_amount) AS total_revenue, COUNT(*) AS reservation_count
                FROM reservations
                WHERE property_id = :property_id
                  AND tenant_id = :tenant_id
                  AND check_in_date >= :period_start
                  AND check_in_date < :period_end
                GROUP BY currency
            """),
            {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "period_start": period_start,
                "period_end": period_end,
            },
        )
        rows = revenue_result.fetchall()

    if not rows:
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": "0.00",
            "currency": "USD",
            "count": 0,
        }
    if len(rows) != 1:
        raise ValueError("Cannot combine revenue recorded in multiple currencies")

    row = rows[0]
    total = Decimal(str(row.total_revenue)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "total": format(total, "f"),
        "currency": row.currency,
        "count": row.reservation_count,
    }
