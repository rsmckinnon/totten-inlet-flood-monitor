"""Atmospheric context and seven-day tide outlooks; never a water-level model."""
import asyncio
import copy
from email.utils import parsedate_to_datetime
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, sources

ATMOSPHERE_URL = 'https://api.open-meteo.com/v1/gfs'
KING_TIDE_FILE = Path(__file__).with_name('king_tides.json')


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def utc(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def pressure_label(value):
    value = finite(value)
    if value is None:
        return None
    if value < 985:
        return 'Extremely low pressure'
    if value < 995:
        return 'Very low pressure'
    if value < 1005:
        return 'Low pressure'
    return 'Normal / modestly low'


def parse_atmosphere(payload):
    hourly = payload.get('hourly', {})
    units = payload.get('hourly_units', {})
    expected = {'pressure_msl': 'hPa', 'wind_speed_10m': 'mp/h', 'wind_direction_10m': '°'}
    if any(units.get(key) != unit for key, unit in expected.items()):
        raise ValueError('Unexpected atmospheric units')
    rows = []
    for i, timestamp in enumerate(hourly.get('time', [])):
        time = utc(timestamp)
        if time is None:
            continue
        row = {'time_utc': time.isoformat()}
        for key in expected:
            values = hourly.get(key, [])
            row[key] = finite(values[i]) if i < len(values) else None
        if row['pressure_msl'] is not None and not 800 <= row['pressure_msl'] <= 1100:
            row['pressure_msl'] = None
        if row['wind_speed_10m'] is not None and row['wind_speed_10m'] < 0:
            row['wind_speed_10m'] = None
        if row['wind_direction_10m'] is not None and not 0 <= row['wind_direction_10m'] <= 360:
            row['wind_direction_10m'] = None
        rows.append(row)
    return sorted(rows, key=lambda row: row['time_utc'])


METNO_URL = 'https://api.met.no/weatherapi/locationforecast/2.0/compact'
METNO_AGENT = 'TottenInletFloodMonitor/1.0 https://github.com/rsmckinnon/totten-inlet-flood-monitor'
_feed_lock = asyncio.Lock()
_feed_cache = {}


def http_time(value):
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def parse_metno(payload):
    properties = payload['properties']
    units = properties['meta']['units']
    expected = {'air_pressure_at_sea_level': 'hPa', 'wind_speed': 'm/s',
                'wind_from_direction': 'degrees'}
    if any(units.get(k) != v for k, v in expected.items()):
        raise ValueError('Unexpected MET Norway units')
    rows = []
    for item in properties.get('timeseries', []):
        time = utc(item.get('time'))
        if time is None:
            continue
        details = item.get('data', {}).get('instant', {}).get('details', {})
        pressure = finite(details.get('air_pressure_at_sea_level'))
        speed = finite(details.get('wind_speed'))
        direction = finite(details.get('wind_from_direction'))
        rows.append({'time_utc': time.isoformat(),
                     'pressure_msl': pressure if pressure is not None and 800 <= pressure <= 1100 else None,
                     'wind_speed_10m': speed * 2.2369362921 if speed is not None and speed >= 0 else None,
                     'wind_direction_10m': direction if direction is not None and 0 <= direction <= 360 else None})
    return sorted(rows, key=lambda row: row['time_utc'])


async def fetch_metno(params, last_modified=None):
    headers = {'User-Agent': METNO_AGENT}
    if last_modified:
        headers['If-Modified-Since'] = last_modified
    async with sources.httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
        response = await client.get(METNO_URL, params=params)
        if response.status_code != 304:
            response.raise_for_status()
        return response


def cache_file(key):
    return sources.CACHE_DIR / f'atmosphere-v2-{key[0]}-{key[1]}.json'


def save_cache(key, entries):
    # Atomic replacement keeps interrupted writes from corrupting the cache.
    try:
        path = cache_file(key)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(entries, allow_nan=False))
        temporary.replace(path)
    except OSError:
        pass  # In-memory cache still protects the providers on read-only storage.


async def atmospheric_forecast():
    """Cache successes and failures; use an independent provider on outage.

    One server worker performs the refresh; concurrent visitors reuse it.
    Provider expiry/Retry-After headers take precedence over our minimum TTLs.
    Expired data are never silently served as fresh forecasts.
    """
    key = (f'{config.PROPERTY_LAT:.4f}', f'{config.PROPERTY_LON:.4f}')
    async with _feed_lock:
        now = datetime.now(timezone.utc)
        stamp = now.timestamp()
        if key not in _feed_cache:
            try:
                loaded = json.loads(cache_file(key).read_text())
                _feed_cache[key] = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                _feed_cache[key] = {}
        entries = _feed_cache[key]
        errors = []
        for provider in ('open_meteo', 'met_norway'):
            entry = entries.get(provider, {})
            if entry.get('retry_at', 0) > stamp:
                data = entry.get('data')
                if data and data.get('available'):
                    return copy.deepcopy(data)
                errors.append(entry.get('reason', provider + ' temporarily unavailable'))
                continue
            response = None
            try:
                if provider == 'open_meteo':
                    response = await sources.get(ATMOSPHERE_URL, params={
                        'latitude': config.PROPERTY_LAT, 'longitude': config.PROPERTY_LON,
                        'hourly': 'pressure_msl,wind_speed_10m,wind_direction_10m',
                        'wind_speed_unit': 'mph', 'timezone': 'GMT', 'forecast_days': 8,
                    })
                    rows = parse_atmosphere(response.json())
                    data = {'source': 'NOAA GFS/HRRR via Open-Meteo',
                            'nearest_tolerance_seconds': 1800}
                else:
                    response = await fetch_metno({'lat': key[0], 'lon': key[1]}, entry.get('last_modified'))
                    if response.status_code == 304:
                        data = copy.deepcopy(entry.get('data') or entry.get('previous_data'))
                        if not data:
                            raise ValueError('MET Norway returned 304 without a cached forecast')
                        rows = data['hourly']
                    else:
                        payload = response.json()
                        rows = parse_metno(payload)
                        data = {'source': 'MET Norway Locationforecast (fallback)',
                                'model_updated_at_utc': payload['properties']['meta'].get('updated_at'),
                                'nearest_tolerance_seconds': 10800}
                if not any(utc(row['time_utc']) >= now and
                           (row.get('pressure_msl') is not None or row.get('wind_speed_10m') is not None)
                           for row in rows):
                    raise ValueError('No usable future atmospheric forecast values')
                data.update(available=True, hourly=rows, retrieved_at_utc=now.isoformat())
                if response.status_code == 304:
                    data['retrieved_at_utc'] = (entry.get('data') or entry['previous_data'])['retrieved_at_utc']
                    data['checked_at_utc'] = now.isoformat()
                entries[provider] = {'data': data,
                    'retry_at': max(stamp + 3600, http_time(response.headers.get('Expires'))),
                    'last_modified': response.headers.get('Last-Modified') or entry.get('last_modified')}
                save_cache(key, entries)
                return copy.deepcopy(data)
            except (sources.httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError) as exc:
                retry_at = stamp + 3600
                if isinstance(exc, sources.httpx.HTTPStatusError):
                    retry = exc.response.headers.get('Retry-After', '')
                    seconds = finite(retry)
                    retry_at = max(retry_at, stamp + seconds if seconds is not None else http_time(retry))
                    reason = f'{provider}: HTTP {exc.response.status_code}'
                else:
                    reason = f'{provider}: {type(exc).__name__}: {exc}'
                # Keep the prior body only for conditional requests after cooldown.
                entries[provider] = {'retry_at': retry_at, 'reason': reason,
                    'last_modified': entry.get('last_modified'),
                    'previous_data': entry.get('data') or entry.get('previous_data')}
                errors.append(reason)
                save_cache(key, entries)
        return {'available': False, 'reason': '; '.join(errors), 'hourly': []}


def context_at(time, atmosphere):
    result = {'pressure_mb': None, 'pressure_label': None, 'wind_text': None,
              'atmosphere_time_utc': None}
    rows = atmosphere.get('hourly', []) if atmosphere.get('available') else []
    rows = [row for row in rows if utc(row.get('time_utc')) is not None]
    if not rows:
        return result
    row = min(rows, key=lambda row: abs((utc(row['time_utc']) - time).total_seconds()))
    if not min(utc(r['time_utc']) for r in rows) <= time <= max(utc(r['time_utc']) for r in rows):
        return result
    tolerance = atmosphere.get('nearest_tolerance_seconds', 1800)
    if abs((utc(row['time_utc']) - time).total_seconds()) > tolerance:
        return result
    pressure = finite(row.get('pressure_msl'))
    # Classify the displayed number so a rounded boundary never contradicts its label.
    pressure = round(pressure, 1) if pressure is not None else None
    speed, direction = finite(row.get('wind_speed_10m')), finite(row.get('wind_direction_10m'))
    wind = None
    if speed is not None:
        compass = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                   'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
        wind = f'{speed:.1f} mph'
        if direction is not None:
            wind = f'From {compass[int((direction % 360 + 11.25) // 22.5) % 16]} · {wind}'
    result.update(pressure_mb=pressure, pressure_label=pressure_label(pressure),
                  wind_text=wind, atmosphere_time_utc=row['time_utc'])
    return result


def king_tide_dates():
    """Only explicitly verified calendar dates; never infer from tide height."""
    try:
        calendar = json.loads(KING_TIDE_FILE.read_text())
        return set(calendar.get('dates', [])) if calendar.get('source_url') else set()
    except (OSError, ValueError, TypeError):
        return set()


def build_events(tides, model, atmosphere, now=None, king_dates=None):
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    king_dates = king_tide_dates() if king_dates is None else king_dates
    start_model, end_model = utc(model.get('forecast_start_utc')), utc(model.get('forecast_end_utc'))
    peaks = []
    if model.get('available') and start_model and end_model:
        peaks = [p for p in model.get('peaks', [])
                 if utc(p.get('time_utc')) is not None and finite(p.get('mllw_ft')) is not None
                 and start_model <= utc(p['time_utc']) <= end_model]
    paired = sources.pair_high_water_events(tides, peaks)
    by_tide = {event['arcadia_time']: event for event in paired}
    events, seen = [], set()
    for tide in tides:
        time = sources.parse_arcadia_time(tide.get('t'))
        height = finite(tide.get('v'))
        if tide.get('type') != 'H' or time is None or height is None or time > end:
            continue
        if time in seen:
            continue
        seen.add(time)
        event = {'arcadia_time': tide['t'], 'arcadia_mllw_ft': round(height, 2),
                 'sscofs_time_utc': None, 'sscofs_mllw_ft': None,
                 'time_difference_minutes': None, 'kind': 'outlook'}
        # A nearby peak must never extend the actual model coverage.
        if start_model and end_model and start_model <= time <= end_model and tide['t'] in by_tide:
            event.update(by_tide[tide['t']])
            event['kind'] = 'forecast'
        # Keep the pair intact until both the astronomical tide and modeled
        # peak have passed. Filtering peaks at 'now' erased a valid height
        # during the gap between these two predictions.
        event_time = utc(event['sscofs_time_utc']) or time
        if max(time, event_time) < now:
            continue
        event['event_time_utc'] = event_time.isoformat()
        event['king_tide'] = time.astimezone(sources.PACIFIC).date().isoformat() in king_dates
        event.update(context_at(event_time, atmosphere))
        events.append(event)
    return sorted(events, key=lambda event: event['event_time_utc'])


def check_rain_coverage(events, rainfall, observed, now=None):
    """Do not present partial forecast rainfall as a complete 72-hour total."""
    now = now or datetime.now(timezone.utc)
    intervals = (rainfall or {}).get('forecast_intervals', [])
    for event in events:
        end = utc(event['event_time_utc'])
        start = end - timedelta(hours=72)
        cursor = max(start, now)
        spans = sorted((utc(i['start_utc']), utc(i['end_utc'])) for i in intervals)
        for left, right in spans:
            if left > cursor:
                break
            if right > cursor:
                cursor = right
        complete = cursor >= end and (start >= now or bool((observed or {}).get('available')))
        event['rainfall_coverage_complete'] = complete
        if not complete:
            event['preceding_72h_rain_in'] = None
    return events
