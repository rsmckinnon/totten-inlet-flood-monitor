# Totten Inlet Flood Monitor — v1

MVP web application for monitoring compound flood risk around Oyster Bay / Totten Inlet, Washington.

## Inputs
- NOAA CO-OPS tide predictions: Arcadia, Totten Inlet (9446666)
- NWS point/hourly/grid forecast data
- Pressure, wind and precipitation signals
- Optional NOAA SSCOFS Totten Inlet Entrance (PUG1544) feed when a stable machine-readable endpoint is configured
- Configurable property-specific flood threshold

## Run

Python 3.10+:

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env
    uvicorn app.main:app --reload

Then open http://127.0.0.1:8000

Edit `.env` to set a representative Oyster Bay latitude/longitude and a descriptive NWS User-Agent.

## Calibration

The initial score is deliberately transparent and provisional. Do not treat its generic thresholds as validated property-flood thresholds. Leave `PROPERTY_FLOOD_THRESHOLD_FT` blank until we calibrate it from observations, beginning with the Dec. 27, 2022 event.

This is a decision-support tool, not a life-safety warning system.

## Seven-day high-water outlook

Upcoming High-Water Periods now shows Arcadia high tides for the next seven
24-hour periods. Eight calendar days are requested so the last rolling day is
covered. Rows within the actual SSCOFS file's time coverage retain the existing
nearest-peak pairing (maximum three hours). Beyond that coverage, or when there
is no matching peak, total water and threshold margin are unavailable. No
pressure-derived height, extrapolated SSCOFS height, or synthetic surge is used.

Atmospheric context comes from NOAA GFS/HRRR through
[Open-Meteo's GFS API](https://open-meteo.com/en/docs/gfs-api), using the configured
property coordinates, sea-level pressure (`pressure_msl`, hPa = mb), and 10 m wind
speed/direction (mph and degrees from north). Open-Meteo uses the nearest hourly forecast within 30 minutes.
If that service fails, MET Norway Locationforecast supplies an independent
fallback using the nearest actual forecast sample within three hours (later
forecasts are six-hourly). The displayed sample time identifies this offset.
No atmospheric interpolation or extrapolation is performed; missing values
and times beyond atmospheric coverage remain unavailable. Retrieval time is shown separately from model issuance.
Forecast uncertainty increases with lead time. NWS remains the rainfall source.
A failure of the atmospheric service does not suppress tide or SSCOFS rows.

Pressure categories are application-specific descriptions, not official NOAA
warning levels or locally calibrated flood predictors:

| Displayed sea-level pressure | Label |
| --- | --- |
| ≥1005 mb | Normal / modestly low |
| ≥995 and <1005 mb | Low pressure |
| ≥985 and <995 mb | Very low pressure |
| <985 mb | Extremely low pressure |

Pressure is displayed to one decimal and the same value determines its label.
Wind shows direction of origin and speed; no uncalibrated wind risk score is
added. Neither pressure, wind, rainfall, nor king-tide metadata changes risk.
Risk still uses only the existing SSCOFS modeled level and property threshold.
The table suppresses 72-hour rainfall totals when forecast intervals do not
cover the required future interval or required observations are unavailable.

`app/king_tides.json` records the Washington Sea Grant 2026–27 Shelton calendar
as nearby regional date metadata (the Olympia dates are identical). Dates use
Pacific time; the tag marks the calendar date, not a specific tide height.
Arcadia remains the source of tide timing and height. Update the calendar from
its linked source each season; absent dates never get inferred from heights.

## Verification and whole-file updates

Run `python -m unittest discover -s tests -v` in the activated environment.
Tests cover horizon boundaries, provider failure, pressure thresholds, units,
missing values, Pacific time, rainfall coverage, API responses, and king-tide
risk invariance. No new Python dependencies are required.

For manual updates, replace each supplied file in full; no line-by-line edits
are needed. Keep `.env`, the existing virtual environment, and NOAA cache files.
Restart the server if it is not running with automatic reload.


## Atmospheric provider resilience

Open-Meteo can return HTTP 429 on a hosted server. The app backs off for at
least one hour (longer when Retry-After requires it), then uses the independent
[MET Norway Locationforecast](https://api.met.no/doc/locationforecast/datamodel)
feed. MET Norway supplies sea-level pressure in hPa and wind in m/s; wind is
converted to mph. Technical details identify the active source, and rows show
the atmospheric sample time. This context never changes SSCOFS heights or risk.
Data attribution: MET Norway and Open-Meteo, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Successful responses and failures are cached for at least one hour. Provider
Expires headers and Retry-After are respected, concurrent visitors share a
refresh, and MET Norway uses If-Modified-Since when revalidating. Cache entries
are scoped to coordinates and atomically saved under `.cache`. A persistent
disk retains them across restarts; without one, the process still caches in
memory. Run one Uvicorn worker as in the existing Render start command.
Expired forecasts are not silently served on errors. No API key is required.
