from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncio
import copy
import csv
import logging
import math
import tempfile
import time
import io
import re

import httpx
import netCDF4

from . import config


PACIFIC = ZoneInfo("America/Los_Angeles")

MM_PER_INCH = 25.4

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------
# Basic HTTP helper
# ---------------------------------------------------------

async def get(url, params=None):
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={
            "User-Agent": config.NWS_USER_AGENT,
        },
    ) as client:
        response = await client.get(
            url,
            params=params,
        )
        response.raise_for_status()
        return response


# ---------------------------------------------------------
# NOAA Arcadia tide predictions
# ---------------------------------------------------------

async def tides(days=8):
    """
    Retrieve NOAA high/low astronomical tide predictions
    for Arcadia, Totten Inlet.

    We request several days so the Arcadia tide series
    comfortably covers the useful SSCOFS forecast horizon.
    """

    now = datetime.now(PACIFIC)

    begin_date = now.strftime("%Y%m%d")

    end_date = (
        now + timedelta(days=days)
    ).strftime("%Y%m%d")

    params = {
        "product": "predictions",
        "application": "TottenInletFloodMonitor",
        "begin_date": begin_date,
        "end_date": end_date,
        "datum": config.COOPS_DATUM,
        "station": config.COOPS_STATION,
        "time_zone": config.COOPS_TIMEZONE,
        "units": config.COOPS_UNITS,
        "interval": "hilo",
        "format": "json",
    }

    response = await get(
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
        params=params,
    )

    payload = response.json()

    return payload.get(
        "predictions",
        [],
    )


# ---------------------------------------------------------
# National Weather Service forecast
# ---------------------------------------------------------

async def nws():
    """
    Retrieve the NWS hourly forecast and raw gridded
    forecast for the property location.
    """

    point_url = (
        "https://api.weather.gov/points/"
        f"{config.PROPERTY_LAT},"
        f"{config.PROPERTY_LON}"
    )

    point_response = await get(point_url)

    point_data = point_response.json()

    properties = point_data["properties"]

    hourly_url = properties["forecastHourly"]
    grid_url = properties["forecastGridData"]

    hourly_response = await get(hourly_url)
    grid_response = await get(grid_url)

    hourly_data = hourly_response.json()
    grid_data = grid_response.json()

    hourly_periods = (
        hourly_data
        .get("properties", {})
        .get("periods", [])
    )

    grid_properties = grid_data.get(
        "properties",
        {},
    )

    return hourly_periods, grid_properties


# ---------------------------------------------------------
# Shelton Airport observed rainfall
# ---------------------------------------------------------

async def shelton_rainfall(hours=72):
    """
    Retrieve hourly precipitation observations from
    Shelton Airport / Sanderson Field (KSHN).

    Iowa Environmental Mesonet provides an hourly
    precipitation product derived from ASOS observations.

    Trace precipitation may appear as 0.0001 inch.
    """

    now = datetime.now(timezone.utc)

    # Extra buffer helps ensure that a complete 72-hour
    # period is available despite observation timing.
    start = now - timedelta(
        hours=hours + 24
    )

    end = now + timedelta(days=1)

    params = {
        "network": "WA_ASOS",
        "station": "SHN",
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "year2": end.year,
        "month2": end.month,
        "day2": end.day,
        "tz": "UTC",
    }

    url = (
        "https://mesonet.agron.iastate.edu/"
        "cgi-bin/request/hourlyprecip.py"
    )

    try:
        response = await get(
            url,
            params=params,
        )

        reader = csv.DictReader(
            io.StringIO(response.text)
        )

        observations = []

        cutoff = now - timedelta(
            hours=hours
        )

        for row in reader:
            station = (
                row.get("station", "")
                .strip()
                .upper()
            )

            if station not in {
                "SHN",
                "KSHN",
            }:
                continue

            valid_text = (
                row.get("valid", "")
                .strip()
            )

            rain_text = (
                row.get("precip_in", "")
                .strip()
            )

            if not valid_text:
                continue

            try:
                valid = datetime.strptime(
                    valid_text,
                    "%Y-%m-%d %H:%M",
                ).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

            if valid < cutoff:
                continue

            if valid > now:
                continue

            try:
                rain = float(rain_text)
            except (
                TypeError,
                ValueError,
            ):
                continue

            observations.append(
                {
                    "time_utc":
                        valid.isoformat(),
                    "rain_in":
                        rain,
                }
            )

        observations.sort(
            key=lambda x:
                x["time_utc"]
        )

        total = sum(
            x["rain_in"]
            for x in observations
        )

        first_time = None
        last_time = None

        if observations:
            first_time = observations[
                0
            ]["time_utc"]

            last_time = observations[
                -1
            ]["time_utc"]

        return {
            "available": True,
            "station": "KSHN",
            "name":
                "Shelton Airport / Sanderson Field",
            "source":
                "Iowa Environmental Mesonet "
                "hourly precipitation",
            "hours": hours,
            "total_in": round(
                total,
                4,
            ),
            "observation_count":
                len(observations),
            "first_observation_utc":
                first_time,
            "last_observation_utc":
                last_time,
            "observations":
                observations,
        }

    except Exception as exc:
        return {
            "available": False,
            "station": "KSHN",
            "name":
                "Shelton Airport / Sanderson Field",
            "source":
                "Iowa Environmental Mesonet "
                "hourly precipitation",
            "hours": hours,
            "reason": str(exc),
            "observations": [],
        }


# ---------------------------------------------------------
# NWS precipitation parsing
# ---------------------------------------------------------

def parse_iso_duration_hours(text):
    """
    Parse the simple ISO durations used by the NWS
    quantitative precipitation grid.

    Examples:
        PT6H
        PT5H
        PT30M
        PT1H30M
    """

    if not text:
        return None

    match = re.fullmatch(
        r"PT(?:(\d+)H)?"
        r"(?:(\d+)M)?",
        text,
    )

    if not match:
        return None

    hours = int(
        match.group(1) or 0
    )

    minutes = int(
        match.group(2) or 0
    )

    duration = (
        hours +
        minutes / 60.0
    )

    if duration <= 0:
        return None

    return duration


def parse_nws_valid_time(valid_time):
    """
    Convert an NWS validTime string such as

        2026-09-19T12:00:00+00:00/PT6H

    into UTC start/end datetimes.
    """

    if not valid_time:
        return None

    try:
        start_text, duration_text = (
            valid_time.split(
                "/",
                1,
            )
        )
    except ValueError:
        return None

    try:
        start = datetime.fromisoformat(
            start_text.replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return None

    if start.tzinfo is None:
        start = start.replace(
            tzinfo=timezone.utc
        )
    else:
        start = start.astimezone(
            timezone.utc
        )

    duration_hours = (
        parse_iso_duration_hours(
            duration_text
        )
    )

    if duration_hours is None:
        return None

    end = start + timedelta(
        hours=duration_hours
    )

    return (
        start,
        end,
        duration_hours,
    )


def parse_nws_precipitation(
    precipitation_grid,
):
    """
    Convert the raw NWS quantitative precipitation
    intervals from millimeters to inches.

    For rolling-window calculations only, each interval's
    accumulation is distributed uniformly over the
    interval. This is interpolation; it does NOT mean the
    NWS predicts constant rainfall throughout the interval.
    """

    if not precipitation_grid:
        return []

    values = precipitation_grid.get(
        "values",
        [],
    )

    intervals = []

    for item in values:
        parsed = parse_nws_valid_time(
            item.get("validTime")
        )

        if parsed is None:
            continue

        (
            start,
            end,
            duration_hours,
        ) = parsed

        value = item.get("value")

        if value is None:
            continue

        try:
            amount_mm = float(value)
        except (
            TypeError,
            ValueError,
        ):
            continue

        amount_in = (
            amount_mm /
            MM_PER_INCH
        )

        hourly_rate_in = (
            amount_in /
            duration_hours
        )

        intervals.append(
            {
                "start_utc":
                    start.isoformat(),
                "end_utc":
                    end.isoformat(),
                "duration_hours":
                    duration_hours,
                "amount_mm":
                    amount_mm,
                "amount_in":
                    amount_in,
                "hourly_rate_in":
                    hourly_rate_in,
            }
        )

    return intervals


def interval_overlap_hours(
    start_a,
    end_a,
    start_b,
    end_b,
):
    """
    Return the number of hours shared by two time
    intervals.
    """

    start = max(
        start_a,
        start_b,
    )

    end = min(
        end_a,
        end_b,
    )

    if end <= start:
        return 0.0

    return (
        end - start
    ).total_seconds() / 3600.0


def forecast_rain_between(
    start,
    end,
    forecast_intervals,
):
    """
    Estimate NWS forecast rainfall between two datetimes
    using interval-overlap interpolation.
    """

    total = 0.0

    for interval in forecast_intervals:
        interval_start = (
            datetime.fromisoformat(
                interval["start_utc"]
            )
        )

        interval_end = (
            datetime.fromisoformat(
                interval["end_utc"]
            )
        )

        overlap_hours = (
            interval_overlap_hours(
                start,
                end,
                interval_start,
                interval_end,
            )
        )

        if overlap_hours <= 0:
            continue

        total += (
            interval[
                "hourly_rate_in"
            ] *
            overlap_hours
        )

    return total


def observed_rain_between(
    start,
    end,
    observations,
):
    """
    Sum Shelton hourly precipitation observations whose
    timestamps fall within the requested interval.
    """

    total = 0.0

    for observation in observations:
        try:
            valid = (
                datetime.fromisoformat(
                    observation[
                        "time_utc"
                    ]
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if (
            valid >= start
            and valid < end
        ):
            try:
                total += float(
                    observation[
                        "rain_in"
                    ]
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

    return total


def rainfall_for_time(
    target_time,
    forecast_intervals,
    observed_rainfall,
    rolling_hours=72,
    now=None,
):
    """
    Calculate preceding rolling rainfall at a specific
    time.

    Before the present time, rainfall comes from Shelton
    observations. After the present time, it comes from
    the NWS property-grid forecast.
    """

    if target_time.tzinfo is None:
        target_time = (
            target_time.replace(
                tzinfo=timezone.utc
            )
        )
    else:
        target_time = (
            target_time.astimezone(
                timezone.utc
            )
        )

    if now is None:
        now = datetime.now(
            timezone.utc
        )

    window_end = target_time

    window_start = (
        window_end -
        timedelta(
            hours=rolling_hours
        )
    )

    observations = []

    if (
        observed_rainfall
        and observed_rainfall.get(
            "available"
        )
    ):
        observations = (
            observed_rainfall.get(
                "observations",
                [],
            )
        )

    observed_end = min(
        window_end,
        now,
    )

    observed_total = 0.0

    if observed_end > window_start:
        observed_total = (
            observed_rain_between(
                window_start,
                observed_end,
                observations,
            )
        )

    forecast_start = max(
        window_start,
        now,
    )

    forecast_total = 0.0

    if window_end > forecast_start:
        forecast_total = (
            forecast_rain_between(
                forecast_start,
                window_end,
                forecast_intervals,
            )
        )

    return {
        "preceding_72h_rain_in":
            round(
                observed_total +
                forecast_total,
                4,
            ),
        "observed_component_in":
            round(
                observed_total,
                4,
            ),
        "forecast_component_in":
            round(
                forecast_total,
                4,
            ),
    }


def build_rainfall_timeline(
    hourly_weather,
    precipitation_grid,
    observed_rainfall,
    rolling_hours=72,
):
    """
    Build rainfall information corresponding to each NWS
    hourly forecast period.

    "Preceding 72-hour rainfall" is evaluated at the
    START of the displayed forecast hour. Rain forecast
    during that hour is kept as a separate value.
    """

    now = datetime.now(
        timezone.utc
    )

    forecast_intervals = (
        parse_nws_precipitation(
            precipitation_grid
        )
    )

    timeline = []

    for period in hourly_weather:
        start_text = period.get(
            "startTime"
        )

        end_text = period.get(
            "endTime"
        )

        if (
            not start_text
            or not end_text
        ):
            continue

        try:
            period_start = (
                datetime.fromisoformat(
                    start_text.replace(
                        "Z",
                        "+00:00",
                    )
                )
            )

            period_end = (
                datetime.fromisoformat(
                    end_text.replace(
                        "Z",
                        "+00:00",
                    )
                )
            )

        except ValueError:
            continue

        period_start_utc = (
            period_start.astimezone(
                timezone.utc
            )
        )

        period_end_utc = (
            period_end.astimezone(
                timezone.utc
            )
        )

        forecast_period_rain = (
            forecast_rain_between(
                period_start_utc,
                period_end_utc,
                forecast_intervals,
            )
        )

        containing_interval = None

        for interval in forecast_intervals:
            interval_start = (
                datetime.fromisoformat(
                    interval[
                        "start_utc"
                    ]
                )
            )

            interval_end = (
                datetime.fromisoformat(
                    interval[
                        "end_utc"
                    ]
                )
            )

            if (
                period_start_utc
                >= interval_start
                and period_start_utc
                < interval_end
            ):
                containing_interval = (
                    interval
                )
                break

        rolling = rainfall_for_time(
            period_start_utc,
            forecast_intervals,
            observed_rainfall,
            rolling_hours=rolling_hours,
            now=now,
        )

        row = {
            "start_time":
                start_text,
            "end_time":
                end_text,
            "forecast_period_rain_in":
                round(
                    forecast_period_rain,
                    4,
                ),
            "preceding_72h_rain_in":
                rolling[
                    "preceding_72h_rain_in"
                ],
            "observed_component_in":
                rolling[
                    "observed_component_in"
                ],
            "forecast_component_in":
                rolling[
                    "forecast_component_in"
                ],
            "forecast_interval_rain_in":
                None,
            "forecast_interval_hours":
                None,
            "forecast_interval_start_utc":
                None,
            "forecast_interval_end_utc":
                None,
        }

        if containing_interval:
            row[
                "forecast_interval_rain_in"
            ] = round(
                containing_interval[
                    "amount_in"
                ],
                4,
            )

            row[
                "forecast_interval_hours"
            ] = containing_interval[
                "duration_hours"
            ]

            row[
                "forecast_interval_start_utc"
            ] = containing_interval[
                "start_utc"
            ]

            row[
                "forecast_interval_end_utc"
            ] = containing_interval[
                "end_utc"
            ]

        timeline.append(row)

    return {
        "rolling_hours":
            rolling_hours,
        "forecast_intervals":
            forecast_intervals,
        "timeline":
            timeline,
    }


# ---------------------------------------------------------
# Water-level peak helpers
# ---------------------------------------------------------

def find_water_level_peaks(
    times,
    levels,
):
    """
    Find local maxima in the SSCOFS total-water-level
    forecast.

    Adjacent forecast points around a broad maximum are
    collapsed into one high-water peak.
    """

    candidates = []

    if len(levels) < 3:
        return candidates

    for i in range(
        1,
        len(levels) - 1,
    ):
        previous = levels[i - 1]
        current = levels[i]
        following = levels[i + 1]

        if (
            current >= previous
            and current > following
        ):
            candidates.append(i)

    # SSCOFS can occasionally produce tiny wiggles near
    # a broad maximum. Keep only the highest candidate
    # within six hours of another candidate.
    selected = []

    for index in candidates:
        if not selected:
            selected.append(index)
            continue

        previous_index = selected[-1]

        time_difference = abs(
            (
                times[index] -
                times[previous_index]
            ).total_seconds()
        )

        if time_difference <= 6 * 3600:
            if (
                levels[index] >
                levels[previous_index]
            ):
                selected[-1] = index
        else:
            selected.append(index)

    return [
        {
            "time_utc":
                times[index]
                .astimezone(
                    timezone.utc
                )
                .isoformat(),
            "mllw_ft":
                round(
                    levels[index],
                    2,
                ),
        }
        for index in selected
    ]


# ---------------------------------------------------------
# Arcadia / SSCOFS pairing
# ---------------------------------------------------------

def parse_arcadia_time(text):
    """
    Arcadia predictions are requested using LST_LDT, so
    NOAA returns local Pacific clock time without an
    explicit UTC offset.
    """

    try:
        local = datetime.strptime(
            text,
            "%Y-%m-%d %H:%M",
        ).replace(
            tzinfo=PACIFIC
        )

        return local.astimezone(
            timezone.utc
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def pair_high_water_events(
    tide_predictions,
    model_peaks,
    max_difference_hours=3,
):
    """
    Pair each SSCOFS high-water peak with the nearest
    Arcadia astronomical high tide.

    The two values describe different locations and
    different physical quantities. The pairing is for
    convenient display, not for calculating "surge."
    """

    arcadia_highs = []

    for tide in tide_predictions:
        if tide.get("type") != "H":
            continue

        tide_time = parse_arcadia_time(
            tide.get("t")
        )

        if tide_time is None:
            continue

        try:
            height = float(
                tide.get("v")
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        arcadia_highs.append(
            {
                "time_utc":
                    tide_time,
                "time_local":
                    tide.get("t"),
                "mllw_ft":
                    height,
            }
        )

    events = []

    for peak in model_peaks:
        try:
            model_time = (
                datetime.fromisoformat(
                    peak[
                        "time_utc"
                    ].replace(
                        "Z",
                        "+00:00",
                    )
                )
            )

            model_level = float(
                peak["mllw_ft"]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue

        if model_time.tzinfo is None:
            model_time = (
                model_time.replace(
                    tzinfo=timezone.utc
                )
            )
        else:
            model_time = (
                model_time.astimezone(
                    timezone.utc
                )
            )

        if not arcadia_highs:
            continue

        nearest = min(
            arcadia_highs,
            key=lambda x:
                abs(
                    (
                        x["time_utc"] -
                        model_time
                    ).total_seconds()
                ),
        )

        difference_minutes = int(
            round(
                (
                    model_time -
                    nearest[
                        "time_utc"
                    ]
                ).total_seconds()
                / 60.0
            )
        )

        if (
            abs(difference_minutes)
            >
            max_difference_hours * 60
        ):
            continue

        events.append(
            {
                "arcadia_time":
                    nearest[
                        "time_local"
                    ],
                "arcadia_mllw_ft":
                    round(
                        nearest[
                            "mllw_ft"
                        ],
                        2,
                    ),
                "sscofs_time_utc":
                    model_time.isoformat(),
                "sscofs_mllw_ft":
                    round(
                        model_level,
                        2,
                    ),
                "time_difference_minutes":
                    difference_minutes,
            }
        )

    events.sort(
        key=lambda x:
            x["sscofs_time_utc"]
    )

    return events


def add_rainfall_to_high_water_events(
    events,
    rainfall,
    observed_rainfall,
):
    """
    Add preceding 72-hour rainfall to each paired
    high-water event.

    This is contextual information only. It does not
    modify the flood-risk classification.
    """

    forecast_intervals = []

    if rainfall:
        forecast_intervals = (
            rainfall.get(
                "forecast_intervals",
                [],
            )
        )

    now = datetime.now(
        timezone.utc
    )

    enriched = []

    for event in events:
        result = dict(event)

        try:
            event_time = (
                datetime.fromisoformat(
                    (event.get("event_time_utc") or event["sscofs_time_utc"]).replace(
                        "Z",
                        "+00:00",
                    )
                )
            )

            rainfall_result = (
                rainfall_for_time(
                    event_time,
                    forecast_intervals,
                    observed_rainfall,
                    rolling_hours=72,
                    now=now,
                )
            )

            result.update(
                rainfall_result
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            result[
                "preceding_72h_rain_in"
            ] = None

            result[
                "observed_component_in"
            ] = None

            result[
                "forecast_component_in"
            ] = None

        enriched.append(result)

    return enriched


# ---------------------------------------------------------
# NOAA SSCOFS
# ---------------------------------------------------------

def sscofs_candidate_cycles():
    """
    Generate recent SSCOFS forecast filenames, newest
    cycle first.

    Operational cycles are expected at
    03, 09, 15, and 21 UTC.
    """

    now = datetime.now(
        timezone.utc
    )

    candidates = []

    for days_back in range(0, 3):
        day = (
            now -
            timedelta(
                days=days_back
            )
        )

        date_text = day.strftime(
            "%Y%m%d"
        )

        for hour in (
            21,
            15,
            9,
            3,
        ):
            cycle_time = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                tzinfo=timezone.utc,
            )

            if cycle_time > now:
                continue

            filename = (
                f"sscofs.t{hour:02d}z."
                f"{date_text}."
                "stations.forecast.nc"
            )

            url = (
                f"{config.SSCOFS_NOMADS_BASE}/"
                f"sscofs.{date_text}/"
                f"{filename}"
            )

            candidates.append(
                {
                    "cycle":
                        cycle_time,
                    "filename":
                        filename,
                    "url":
                        url,
                }
            )

    candidates.sort(
        key=lambda x:
            x["cycle"],
        reverse=True,
    )

    return candidates


# One small processed forecast per worker; never retain netCDF/NumPy objects.
SSCOFS_CACHE_TTL_SECONDS = 30 * 60
SSCOFS_ERROR_TTL_SECONDS = 60
_sscofs_lock = asyncio.Lock()
_sscofs_cache = {}
_log = logging.getLogger(__name__)


async def _download_sscofs_to_path(url, path):
    """Stream to a unique temporary file, then atomically publish it.

    A cancelled or failed transfer cannot leave a partial .nc cache entry.
    Avoid response.content: the station file is about 47 MB.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=path.name + ".", suffix=".part", delete=False
        ) as output:
            temporary = Path(output.name)
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True,
                headers={"User-Agent": config.NWS_USER_AGENT},
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        output.write(chunk)
        if not temporary.stat().st_size:
            raise OSError("Empty SSCOFS download")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def download_sscofs_file():
    """Find the newest available cycle, reusing complete files already on disk.

    Called under _sscofs_lock by sscofs(). An explicit SSCOFS_ENDPOINT
    continues to select a fixed file, as before.
    """
    if config.SSCOFS_ENDPOINT:
        url = config.SSCOFS_ENDPOINT
        filename = url.rstrip("/").split("/")[-1]
        path = CACHE_DIR / filename
        if not path.exists() or not path.stat().st_size:
            await _download_sscofs_to_path(url, path)
        return {"path": path, "filename": filename, "cycle": None}

    for candidate in sscofs_candidate_cycles():
        path = CACHE_DIR / candidate["filename"]
        try:
            if not path.exists() or not path.stat().st_size:
                await _download_sscofs_to_path(candidate["url"], path)
            return {"path": path, "filename": candidate["filename"],
                    "cycle": candidate["cycle"]}
        except (httpx.HTTPError, OSError):
            continue
    raise RuntimeError("No recent NOAA SSCOFS forecast file was available.")


def _cleanup_sscofs_files(downloaded):
    """After successful parsing, keep this cycle and its closest predecessor.

    Only older operational station forecasts in our cache are eligible.
    Preserve custom endpoints, future cycles, other caches and research files.
    Cleanup failure must not turn a usable forecast into a failed request.
    """
    if downloaded["cycle"] is None:
        return
    try:
        older = []
        for path in CACHE_DIR.glob("sscofs.t*z.*.stations.forecast.nc"):
            match = re.fullmatch(
                r"sscofs\.t(03|09|15|21)z\.(\d{8})\.stations\.forecast\.nc",
                path.name,
            )
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                cycle = datetime.strptime(
                    match[2] + match[1], "%Y%m%d%H"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if cycle < downloaded["cycle"]:
                older.append((cycle, path))
        older.sort(reverse=True)
        for _, path in older[1:]:
            path.unlink(missing_ok=True)
    except OSError:
        _log.warning("Could not clean older SSCOFS cache files", exc_info=True)


def decode_station_name(
    station_name_variable,
    station_index,
):
    """
    Decode the station name from a netCDF character
    array.
    """

    raw = (
        station_name_variable[
            station_index
        ]
    )

    try:
        value = netCDF4.chartostring(
            raw
        )

        if hasattr(
            value,
            "item",
        ):
            value = value.item()

        if isinstance(
            value,
            bytes,
        ):
            value = value.decode(
                "utf-8",
                errors="ignore",
            )

        return str(value).strip(
            "\x00 "
        )

    except Exception:
        try:
            if hasattr(
                raw,
                "tobytes",
            ):
                return (
                    raw.tobytes()
                    .decode(
                        "utf-8",
                        errors="ignore",
                    )
                    .strip(
                        "\x00 "
                    )
                )
        except Exception:
            pass

    return "Totten"


def _process_sscofs_file(downloaded):
    """Read only Totten station 93, time, and its station label.

    The Dataset closes even on decoding errors. All arrays and variables
    are local to this function; only ordinary Python values leave it.
    """
    path = downloaded["path"]

    with netCDF4.Dataset(
        path,
        "r",
    ) as dataset:

        station_index = (
            config.SSCOFS_STATION_INDEX
        )

        zeta_variable = (
            dataset.variables["zeta"]
        )

        time_variable = (
            dataset.variables["time"]
        )

        # Bound the HDF5 per-variable chunk cache on small Render workers.
        zeta_variable.set_var_chunk_cache(1024 * 1024, 1009, 0.75)

        raw_levels = (
            zeta_variable[
                :,
                station_index
            ]
        )

        converted_times = (
            netCDF4.num2date(
                time_variable[:],
                units=
                    time_variable.units,
                calendar=getattr(
                    time_variable,
                    "calendar",
                    "standard",
                ),
                only_use_cftime_datetimes=False,
                only_use_python_datetimes=True,
            )
        )

        times = []

        levels = []

        for raw_time, raw_level in zip(
            converted_times,
            raw_levels,
        ):
            if (
                hasattr(
                    raw_level,
                    "mask",
                )
                and raw_level.mask
            ):
                continue

            try:
                level_m = float(
                    raw_level
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            if not math.isfinite(level_m):
                continue

            if raw_time.tzinfo is None:
                time_utc = (
                    raw_time.replace(
                        tzinfo=timezone.utc
                    )
                )
            else:
                time_utc = (
                    raw_time.astimezone(
                        timezone.utc
                    )
                )

            # mllwtomsl is MLLW relative to MSL.
            #
            # Totten:
            #     -2.526926 m
            #
            # MLLW height =
            #     model zeta - mllwtomsl
            #
            level_mllw_m = (
                level_m -
                config.SSCOFS_MLLW_TO_MSL_M
            )

            level_mllw_ft = (
                level_mllw_m *
                3.28084
            )

            times.append(
                time_utc
            )

            levels.append(
                level_mllw_ft
            )

        if not levels:
            raise RuntimeError(
                "SSCOFS file contained "
                "no usable Totten water "
                "levels."
            )

        peak_index = max(
            range(len(levels)),
            key=lambda i:
                levels[i],
        )

        peaks = (
            find_water_level_peaks(
                times,
                levels,
            )
        )

        station_name = "Totten"

        if (
            "station_name"
            in dataset.variables
        ):
            station_name = (
                decode_station_name(
                    dataset.variables[
                        "station_name"
                    ],
                    station_index,
                )
            )

    peak_level = levels[
        peak_index
    ]

    peak_time = times[
        peak_index
    ]

    threshold = (
        config.PROPERTY_FLOOD_THRESHOLD_FT
    )

    margin = None

    if threshold is not None:
        margin = (
            peak_level -
            threshold
        )

    cycle = downloaded[
        "cycle"
    ]

    cycle_text = None

    if cycle is not None:
        cycle_text = (
            cycle.isoformat()
        )

    return {
        "available": True,
        "forecast_start_utc": min(times).isoformat(),
        "forecast_end_utc": max(times).isoformat(),
        "station":
            station_name,
        "station_index":
            config.SSCOFS_STATION_INDEX,
        "cycle_utc":
            cycle_text,
        "file":
            downloaded[
                "filename"
            ],
        "peak_time_utc":
            peak_time.isoformat(),
        "peak_mllw_ft":
            round(
                peak_level,
                2,
            ),
        "peaks":
            peaks,
        "threshold_ft":
            threshold,
        "margin_ft":
            None
            if margin is None
            else round(
                margin,
                2,
            ),
    }


def _sscofs_config_key():
    return (config.SSCOFS_ENDPOINT, config.SSCOFS_NOMADS_BASE,
            config.SSCOFS_STATION_INDEX, config.SSCOFS_MLLW_TO_MSL_M,
            config.PROPERTY_FLOOD_THRESHOLD_FT)


async def sscofs():
    """Share a processed forecast for 30 minutes, then look for a newer cycle.

    The async lock covers discovery, download, processing and cache updates.
    Waiting requests recheck the cache inside the lock. An unchanged file
    reuses its processed result even after TTL expiry. Failures are briefly
    cached to avoid a retry storm; expired forecasts are never served as live.
    """
    async with _sscofs_lock:
        key = _sscofs_config_key()
        if _sscofs_cache.get("key") != key:
            _sscofs_cache.clear()
            _sscofs_cache["key"] = key
        if time.monotonic() < _sscofs_cache.get("expires_at", 0):
            return copy.deepcopy(_sscofs_cache["result"])
        downloaded = None
        processing = False
        try:
            downloaded = await download_sscofs_file()
            stat = downloaded["path"].stat()
            identity = (str(downloaded["path"].resolve()), stat.st_size,
                        stat.st_mtime_ns)
            if identity == _sscofs_cache.get("identity"):
                result = _sscofs_cache["processed"]
            else:
                processing = True
                result = _process_sscofs_file(downloaded)
                processing = False
                _sscofs_cache.update(identity=identity, processed=result)
                _log.info("SSCOFS processed %s for station %s",
                          downloaded["filename"], config.SSCOFS_STATION_INDEX)
            remaining = (datetime.fromisoformat(result["forecast_end_utc"])
                         - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise RuntimeError("SSCOFS forecast coverage has expired.")
            ttl = min(SSCOFS_CACHE_TTL_SECONDS, remaining)
            _cleanup_sscofs_files(downloaded)
        except Exception as exc:
            # Re-download an unreadable file on the next attempt. Do not
            # discard valid files simply because their forecast has expired.
            if processing and downloaded is not None:
                try:
                    downloaded["path"].unlink(missing_ok=True)
                except OSError:
                    pass
            result = {"available": False, "reason": str(exc)}
            ttl = SSCOFS_ERROR_TTL_SECONDS
            _log.warning("SSCOFS unavailable: %s", exc)
        _sscofs_cache.update(result=result, expires_at=time.monotonic() + ttl)
        return copy.deepcopy(result)
