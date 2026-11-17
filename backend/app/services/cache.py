import json
import redis.asyncio as redis
from typing import Dict, Any
import os

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

def revenue_cache_key(property_id: str, tenant_id: str, year: int, month: int) -> str:
    """Create the one canonical tenant- and period-scoped revenue cache key."""
    return f"revenue:{tenant_id}:{property_id}:{year:04d}-{month:02d}"


async def get_revenue_summary(
    property_id: str, tenant_id: str, year: int, month: int
) -> Dict[str, Any]:
    cache_key = revenue_cache_key(property_id, tenant_id, year, month)

    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    from app.services.reservations import calculate_monthly_revenue_summary

    result = await calculate_monthly_revenue_summary(property_id, tenant_id, year, month)
    await redis_client.setex(cache_key, 300, json.dumps(result))
    return result
