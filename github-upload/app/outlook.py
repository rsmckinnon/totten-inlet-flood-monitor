"""Atmospheric context and seven-day tide outlooks; never a water-level model."""
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


async def atmospheric_forecast():
    try:
        response = await sources.get(ATMOSPHERE_URL, params={
            'latitude': config.PROPERTY_LAT, 'longitude': config.PROPERTY_LON,
            'hourly': 'pressure_msl,wind_speed_10m,wind_direction_10m',
            'wind_speed_unit': 'mph', 'timezone': 'GMT', 'forecast_days': 8,
        })
        rows = parse_atmosphere(response.json())
        return {'available': bool(rows), 'source': 'NOAA GFS/HRRR via Open-Meteo',
                'retrieved_at_utc': datetime.now(timezone.utc).isoformat(), 'hourly': rows}
    except (sources.httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        return {'available': False, 'reason': str(exc), 'hourly': []}


def context_at(time, atmosphere):
    result = {'pressure_mb': None, 'pressure_label': None, 'wind_text': None,
              'atmosphere_time_utc': None}
    rows = atmosphere.get('hourly', []) if atmosphere.get('available') else []
    rows = [row for row in rows if utc(row.get('time_utc')) is not None]
    if not rows:
        return result
    row = min(rows, key=lambda row: abs((utc(row['time_utc']) - time).total_seconds()))
    if abs((utc(row['time_utc']) - time).total_seconds()) > 1800:
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
                 and max(now, start_model) <= utc(p['time_utc']) <= end_model]
    paired = sources.pair_high_water_events(tides, peaks)
    by_tide = {event['arcadia_time']: event for event in paired}
    events, seen = [], set()
    for tide in tides:
        time = sources.parse_arcadia_time(tide.get('t'))
        height = finite(tide.get('v'))
        if tide.get('type') != 'H' or time is None or height is None or not now <= time <= end:
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
        event_time = utc(event['sscofs_time_utc']) or time
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
