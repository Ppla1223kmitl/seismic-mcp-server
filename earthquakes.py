from asyncio import TaskGroup
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from math import asin, cos, isfinite, radians, sin, sqrt

import httpx


USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"
THAILAND = timezone(timedelta(hours=7))
DEFAULT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},
    {"max_distance_km": 100, "payout_percent": 0.25},
    {"max_distance_km": 200, "payout_percent": 0.1},
]


class UpstreamError(Exception):
    pass


def parse_time(value, default_timezone=timezone.utc):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=default_timezone)


def number(value, minimum=None, maximum=None):
    value = float(value)
    if not isfinite(value):
        raise ValueError("Expected a finite number")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError(f"Number outside allowed range [{minimum}, {maximum}]")
    return value


async def fetch_events(client, url, params, collection, normalize):
    provider = "USGS" if url == USGS_URL else "TMD"
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise UpstreamError(f"{provider} returned HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise UpstreamError(f"{provider} request failed ({type(exc).__name__})") from exc

    if response.status_code == 204:
        return []
    try:
        payload = response.json()
        if collection == "DailySeismicEvent" and collection not in payload:
            items = payload["DailyEarthquakes"]
            if not isinstance(items, list):
                raise ValueError("Expected an event array")
            day = date.fromisoformat(params["Date"])
            return [
                tmd_event(item)
                for item in items
                if tmd_time(item).astimezone(THAILAND).date() == day
            ]
        items = payload[collection]
        if not isinstance(items, list):
            raise ValueError("Expected an event array")
        return [normalize(item) for item in items]
    except (AttributeError, IndexError, KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
        raise UpstreamError(f"{provider} returned an invalid event payload") from exc


def usgs_event(feature):
    props = feature["properties"]
    lon, lat, depth = feature["geometry"]["coordinates"]
    timestamp = datetime.fromtimestamp(number(props["time"]) / 1000, timezone.utc)
    return {
        "id": str(feature["id"]),
        "time": timestamp.isoformat(),
        "place": props["place"] or "",
        "magnitude": number(props["mag"]),
        "latitude": number(lat, -90, 90),
        "longitude": number(lon, -180, 180),
        "depth_km": number(depth),
        "source": "USGS",
        "url": props.get("url"),
        "raw": feature,
    }


def tmd_time(item):
    local = item.get("DateTime") or item.get("DateTimeThai")
    if local:
        return parse_time(local, THAILAND)
    return parse_time(item["DateTimeUTC"])


def tmd_event(item):
    timestamp = tmd_time(item)
    latitude = number(item["Latitude"], -90, 90)
    longitude = number(item["Longitude"], -180, 180)
    event_id = item.get("EventID")
    if event_id is None:
        identity = f"{timestamp.astimezone(timezone.utc).isoformat()}|{latitude}|{longitude}"
        event_id = sha256(identity.encode()).hexdigest()[:24]
    return {
        "id": f"TMD-{event_id}",
        "time": timestamp.isoformat(),
        "place": item.get("Location") or item.get("OriginThai") or item.get("TitleThai") or "",
        "magnitude": number(item["Magnitude"]),
        "latitude": latitude,
        "longitude": longitude,
        "depth_km": number(item["DepthKm"] if "DepthKm" in item else item["Depth"]),
        "source": "TMD_DailySeismicEvent",
        "url": item.get("URL") or item.get("Url") or item.get("url"),
        "raw": item,
    }


async def usgs_search_earthquakes(client, starttime, endtime, minmagnitude=4.5, limit=20, **filters):
    if parse_time(starttime) > parse_time(endtime):
        raise ValueError("starttime must be on or before endtime")
    if "maxmagnitude" in filters and filters["maxmagnitude"] < minmagnitude:
        raise ValueError("maxmagnitude must be at least minmagnitude")
    radius_keys = {"latitude", "longitude", "maxradiuskm"}
    if radius_keys.intersection(filters) and not radius_keys.issubset(filters):
        raise ValueError("latitude, longitude and maxradiuskm must be supplied together")
    for key, minimum, maximum in [("latitude", -90, 90), ("longitude", -180, 180), ("maxradiuskm", 0, None)]:
        if key in filters:
            number(filters[key], minimum, maximum)
    query = {
        "format": "geojson",
        "starttime": starttime,
        "endtime": endtime,
        "minmagnitude": minmagnitude,
        "limit": int(limit),
        **filters,
    }
    events = await fetch_events(client, USGS_URL, query, "features", usgs_event)
    return {"events": events, "query": query}


async def fetch_tmd_day(client, day):
    params = {"uid": "api", "ukey": "api12345", "format": "json", "Date": day}
    return await fetch_events(client, TMD_URL, params, "DailySeismicEvent", tmd_event)


async def tmd_get_daily_earthquakes(client, date=None):
    day = date if date is not None else datetime.now().date().isoformat()
    if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
        raise ValueError("date must use YYYY-MM-DD")
    return {"events": await fetch_tmd_day(client, day), "query": {"date": day}}


async def tmd_get_recent_earthquakes(client, days=3):
    today = date.today()
    days = int(days)
    try:
        async with TaskGroup() as group:
            tasks = [
                group.create_task(fetch_tmd_day(client, (today - timedelta(days=offset)).isoformat()))
                for offset in range(days)
            ]
    except* UpstreamError as errors:
        raise errors.exceptions[0]

    events = {}
    for task in tasks:
        for event in task.result():
            events.setdefault(event["id"], event)
    ordered = sorted(events.values(), key=lambda event: parse_time(event["time"]), reverse=True)
    return {"events": ordered, "query": {"days": days}}


def distance_km(event, asset):
    lat1, lat2 = (radians(number(point["latitude"], -90, 90)) for point in (event, asset))
    lon1, lon2 = (radians(number(point["longitude"], -180, 180)) for point in (event, asset))
    haversine = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(min(1, haversine)))


def asset_details(asset):
    return {"asset_id": asset["asset_id"], "name": asset.get("name"), "sum_insured": asset.get("sum_insured")}


async def match_events_to_assets(client, events, assets, max_distance_km=100, min_magnitude=4.5):
    number(max_distance_km, 0)
    eligible = [event for event in events if event["magnitude"] >= min_magnitude]
    matches = []
    for asset in assets:
        nearby = []
        for event in eligible:
            distance = distance_km(event, asset)
            if distance <= max_distance_km:
                nearby.append({"event": event, "distance_km": distance})
        if nearby:
            matches.append({**asset_details(asset), "events": nearby})
    return {"matches": matches, "parameters": {"max_distance_km": max_distance_km, "min_magnitude": min_magnitude}}


async def compute_parametric_payout(
    client, event, assets, min_magnitude_trigger=5.0, max_distance_km_trigger=200, payout_curve=None
):
    number(max_distance_km_trigger, 0)
    curve = DEFAULT_CURVE if payout_curve is None else payout_curve
    for band in curve:
        number(band["max_distance_km"], 0)
        number(band["payout_percent"], 0, 1)
    payouts = []
    magnitude = event["magnitude"]
    for asset in assets:
        number(asset["sum_insured"], 0)
        distance = distance_km(event, asset)
        percent = 0
        if magnitude >= min_magnitude_trigger and distance <= max_distance_km_trigger:
            percent = next((band["payout_percent"] for band in curve if distance <= band["max_distance_km"]), 0)
        payouts.append({
            **asset_details(asset),
            "distance_km": distance,
            "payout_percent": percent,
            "payout_amount": asset["sum_insured"] * percent,
        })
    return {
        "event_id": event["id"],
        "event_magnitude": magnitude,
        "parameters": {
            "min_magnitude_trigger": min_magnitude_trigger,
            "max_distance_km_trigger": max_distance_km_trigger,
            "payout_curve": curve,
        },
        "asset_payouts": payouts,
        "total_payout": sum(payout["payout_amount"] for payout in payouts),
    }
