from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import config, sources, risk, outlook


app = FastAPI(title="Totten Inlet Flood Monitor")


@app.get("/api/data")
async def data():
    t = await sources.tides()
    hourly, grid = await sources.nws()
    model = await sources.sscofs()
    observed_rainfall = await sources.shelton_rainfall()

    rainfall = sources.build_rainfall_timeline(
        hourly,
        grid.get(config.RAINFALL_GRID_FIELD),
        observed_rainfall,
    )

    atmosphere = await outlook.atmospheric_forecast()
    high_water_events = outlook.build_events(t, model, atmosphere)
    high_water_events = sources.add_rainfall_to_high_water_events(
        high_water_events, rainfall, observed_rainfall,
    )
    high_water_events = outlook.check_rain_coverage(
        high_water_events, rainfall, observed_rainfall,
    )

    modeled_water_ft = None

    if model.get("available"):
        modeled_water_ft = model.get("peak_mllw_ft")

    risk_result = risk.build_risk(
        modeled_water_ft=modeled_water_ft,
        flood_threshold_ft=config.PROPERTY_FLOOD_THRESHOLD_FT,
    )

    return {
        "tides": t,
        "atmosphere": atmosphere,
        "weather": hourly[:72],
        "grid_precipitation": grid.get(
            config.RAINFALL_GRID_FIELD
        ),
        "sscofs": model,
        "observed_rainfall": observed_rainfall,
        "rainfall": rainfall,
        "high_water_events": high_water_events,
        "risk": risk_result,
        "property_threshold_ft": config.PROPERTY_FLOOD_THRESHOLD_FT,
    }


@app.get("/api/risk")
async def risk_data():
    model = await sources.sscofs()

    modeled_water_ft = None

    if model.get("available"):
        modeled_water_ft = model.get("peak_mllw_ft")

    return risk.build_risk(
        modeled_water_ft=modeled_water_ft,
        flood_threshold_ft=config.PROPERTY_FLOOD_THRESHOLD_FT,
    )


PAGE = '''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">

<title>Totten Inlet Flood Monitor</title>

<style>

body {
    font-family: system-ui, -apple-system, sans-serif;
    max-width: 1100px;
    margin: auto;
    padding: 24px;
    background: #f3f5f7;
    color: #17202a;
}

h1 {
    margin-bottom: 4px;
}

h2 {
    margin-top: 0;
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(220px, 1fr));
    gap: 14px;
}

.card {
    background: white;
    padding: 18px;
    border-radius: 12px;
    box-shadow: 0 1px 4px #0001;
}

.big {
    font-size: 2rem;
    font-weight: 700;
}

#risk {
    font-size: clamp(2rem, 4vw, 3.5rem);
    font-weight: 750;
    line-height: 1.1;
    margin: 12px 0;
}

#risk.muted {
    font-size: 2rem;
}

.water-level {
    font-size: 2.8rem;
    font-weight: 750;
    margin: 4px 0;
}

.muted {
    color: #68737d;
}

.good {
    color: #26734d;
}

.warning {
    color: #a35d00;
}

.danger {
    color: #b42318;
}

.status-message {
    font-size: 1.35rem;
    font-weight: 650;
    margin: 12px 0 4px 0;
}

.margin-message {
    font-size: 1.15rem;
    font-weight: 600;
    margin-bottom: 18px;
}

.table-wrap {
    overflow-x: auto;
}

table {
    width: 100%;
    border-collapse: collapse;
}

td, th {
    padding: 10px 8px;
    border-bottom: 1px solid #eee;
    text-align: left;
    vertical-align: top;
}

.closest-event {
    background: #f7f4ed;
}

.event-level {
    font-weight: 700;
}

button {
    padding: 10px 14px;
    border: 0;
    border-radius: 8px;
    background: #17202a;
    color: white;
    cursor: pointer;
}

.details {
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid #eee;
    line-height: 1.6;
}

details {
    margin-top: 8px;
}

summary {
    cursor: pointer;
    font-weight: 600;
}

.note {
    font-size: .9rem;
    line-height: 1.5;
}

.rain-summary {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 18px;
}

.rain-summary-box {
    background: #f6f8fa;
    padding: 12px;
    border-radius: 8px;
}

.rain-number {
    font-size: 1.35rem;
    font-weight: 700;
    margin-top: 3px;
}

@media (max-width: 700px) {
    body {
        padding: 14px;
    }

    .water-level {
        font-size: 2.3rem;
    }

    table {
        font-size: .88rem;
    }
}

</style>
</head>

<body>

<h1>Totten Inlet Flood Monitor</h1>

<p class="muted">
    Oyster Bay property flood forecast
</p>

<button onclick="load()">Refresh forecast</button>


<div class="grid" style="margin-top:16px">

    <div class="card">
        <span class="muted">Flood risk</span>
        <div id="risk" class="big" aria-live="polite">—</div>
    </div>

    <div class="card">
        <span class="muted">
            Local tide reference
        </span>
        <div class="big">Arcadia</div>
        <div>
            Astronomical tide prediction
        </div>
    </div>

    <div class="card">
        <span class="muted">
            Your flooding threshold
        </span>
        <div id="threshold" class="big">
            —
        </div>
        <div class="muted">
            Provisional property threshold
        </div>
    </div>

</div>


<div class="card" style="margin-top:14px">

    <h2>
        Forecast Total Water Level —
        Totten Inlet
    </h2>

    <div
        id="model-unavailable"
        style="display:none"
    ></div>

    <div
        id="model-data"
        style="display:none"
    >

        <div
            id="water-status"
            class="status-message"
        ></div>

        <div
            id="model-margin"
            class="margin-message"
        ></div>

        <div class="muted">
            Highest modeled total water level
        </div>

        <div
            id="model-peak"
            class="water-level"
        >
            —
        </div>

        <div>
            Expected
            <strong id="model-time">
                —
            </strong>
        </div>

        <p class="muted note">
            This is a modeled total water level.
            It includes the effects of the
            astronomical tide and weather-driven
            ocean conditions, so it may differ
            from the Arcadia astronomical tide.
        </p>

        <p class="muted note">
            Water levels are measured using
            NOAA's standard local tide reference.
            The property flooding threshold is
            provisional and can be refined as
            we collect observations.
        </p>

    </div>
</div>


<div class="card" style="margin-top:14px">

    <h2>Upcoming High-Water Periods</h2>

    <div
        id="high-water-unavailable"
        class="muted"
        style="display:none"
    >
        No high-water periods are
        currently available.
    </div>

    <div class="table-wrap">
        <table id="high-water-events"></table>
    </div>

</div>


<div class="card" style="margin-top:14px">

    <h2>
        Weather and Rainfall
    </h2>

    <div class="rain-summary">

        <div class="rain-summary-box">
            <div class="muted">
                Rain observed — past 72 hours
            </div>
            <div
                id="observed-rain"
                class="rain-number"
            >
                —
            </div>
            <div class="muted note">
                Shelton Airport / Sanderson Field
            </div>
        </div>

        <div class="rain-summary-box">
            <div class="muted">
                Rain forecast — next 72 hours
            </div>
            <div
                id="forecast-rain"
                class="rain-number"
            >
                —
            </div>
            <div class="muted note">
                NWS grid forecast at the property
            </div>
        </div>

    </div>

    <p class="muted note">
        The preceding 72-hour rainfall combines
        observed rainfall from Shelton Airport
        with NWS forecast rainfall as the forecast
        moves forward in time.
    </p>

    <div class="table-wrap">
        <table id="weather"></table>
    </div>

</div>


<div class="card" style="margin-top:14px">
    <h2>Forecast updated</h2>
    <strong id="model-cycle">—</strong>
</div>


<div class="card" style="margin-top:14px">

    <details>

        <summary>
            Technical details
        </summary>

        <div class="details muted">

            <h3>Upcoming High-Water Periods</h3>
    <p class="muted note">
        Next seven days: SSCOFS modeled total water where available,
        followed by Arcadia tide outlooks. Outlooks have no total-water
        estimate or threshold margin. Pressure and wind are context only;
        uncertainty increases farther ahead. Rainfall covers the preceding
        72 hours; incomplete coverage is shown as unavailable.
    </p>
    <p class="muted note">
        Atmospheric data: <a href="https://open-meteo.com/en/docs/gfs-api">NOAA GFS/HRRR via Open-Meteo</a>,
        with <a href="https://api.met.no/doc/locationforecast/datamodel">MET Norway Locationforecast</a>
        as an independent fallback. Forecast data are shared under
        <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>.
        We convert wind to mph and select the nearest forecast sample without interpolation.
        Atmospheric forecasts are cached for at least one hour.
        Wind direction is where wind comes from, at 10 m above ground.
        Pressure labels are application categories, not NOAA warnings:
        normal / modestly low ≥1005 mb; low 995–&lt;1005 mb;
        very low 985–&lt;995 mb; extremely low &lt;985 mb (1 mb = 1 hPa).
        King-tide tags use the <a href="https://waseagrant.uw.edu/our-programs/coastal-hazards-climate-resilience/king-tides/king-tides-calendar/">Washington Sea Grant Shelton calendar</a>
        (2026–27 regional dates) and never change risk.
    </p>
    <p id="atmosphere-status" class="muted note"></p>
    <p class="muted note">
        Flood risk above applies only to available SSCOFS guidance.
    </p>


            <div>
                Tide prediction:
                NOAA Arcadia station 9446666
            </div>

            <div>
                Ocean forecast:
                NOAA Salish Sea and Columbia
                River Operational Forecast
                System (SSCOFS)
            </div>

            <div>
                Forecast location:
                Totten Inlet Entrance
            </div>

            <div>
                SSCOFS station index: 93
            </div>

            <div>
                NOAA model point: PUG1544
            </div>

            <div>
                Water-level datum:
                Mean Lower Low Water (MLLW)
            </div>

            <div>
                Observed rainfall:
                Shelton Airport / Sanderson Field
                (KSHN)
            </div>

            <div>
                Forecast rainfall:
                NWS quantitative precipitation
                forecast at the property grid point
            </div>

            <div id="technical-file"></div>

        </div>

    </details>
</div>


<script>

function escapeText(value) {
    const el = document.createElement('span');
    el.textContent = value == null ? 'Unavailable' : String(value);
    return el.innerHTML;
}

function localTime(iso) {
    if (!iso) return '—';

    const d = new Date(iso);

    return d.toLocaleString(
        undefined,
        {
            weekday: 'short',
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
            timeZoneName: 'short', timeZone: 'America/Los_Angeles'
        }
    );
}


function tideTime(text) {
    if (!text) return '—';

    const parts = text.split(' ');

    if (parts.length !== 2) {
        return text;
    }

    const dateParts = parts[0].split('-');
    const timeParts = parts[1].split(':');

    if (
        dateParts.length !== 3 ||
        timeParts.length !== 2
    ) {
        return text;
    }

    const d = new Date(
        Number(dateParts[0]),
        Number(dateParts[1]) - 1,
        Number(dateParts[2]),
        Number(timeParts[0]),
        Number(timeParts[1])
    );

    return d.toLocaleString(
        undefined,
        {
            weekday: 'short',
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        }
    );
}


function marginText(
    modeledLevel,
    threshold
) {
    if (
        modeledLevel == null ||
        threshold == null
    ) {
        return '—';
    }

    const difference =
        modeledLevel - threshold;

    if (difference >= 0) {
        return (
            difference.toFixed(2) +
            ' ft above'
        );
    }

    return (
        Math.abs(difference).toFixed(2) +
        ' ft below'
    );
}


function marginClass(
    modeledLevel,
    threshold
) {
    if (
        modeledLevel == null ||
        threshold == null
    ) {
        return '';
    }

    const difference =
        modeledLevel - threshold;

    if (difference >= 0) {
        return 'danger';
    }

    if (difference >= -1.5) {
        return 'warning';
    }

    return 'good';
}


function inches(value, digits=2) {
    if (
        value == null ||
        Number.isNaN(Number(value))
    ) {
        return '—';
    }

    return (
        Number(value).toFixed(digits) +
        ' in'
    );
}


function forecastIntervalText(row) {
    if (
        !row ||
        row.forecast_interval_rain_in == null ||
        row.forecast_interval_hours == null
    ) {
        return '—';
    }

    const hours =
        Number(row.forecast_interval_hours);

    const hourText =
        Number.isInteger(hours)
        ? hours.toFixed(0)
        : hours.toFixed(1);

    return (
        Number(
            row.forecast_interval_rain_in
        ).toFixed(2) +
        ' in / ' +
        hourText +
        ' hr'
    );
}


function forecastRainNext72(rainfall) {
    if (
        !rainfall ||
        !rainfall.forecast_intervals
    ) {
        return null;
    }

    const now = new Date();

    const end = new Date(
        now.getTime() +
        72 * 60 * 60 * 1000
    );

    let total = 0;
    let found = false;

    rainfall.forecast_intervals.forEach(
        interval => {
            const start =
                new Date(interval.start_utc);

            const finish =
                new Date(interval.end_utc);

            const overlapStart =
                start > now
                ? start
                : now;

            const overlapEnd =
                finish < end
                ? finish
                : end;

            if (overlapEnd <= overlapStart) {
                return;
            }

            const intervalHours =
                (finish - start) /
                (1000 * 60 * 60);

            const overlapHours =
                (overlapEnd - overlapStart) /
                (1000 * 60 * 60);

            if (intervalHours <= 0) {
                return;
            }

            total +=
                Number(interval.amount_in) *
                (
                    overlapHours /
                    intervalHours
                );

            found = true;
        }
    );

    return found ? total : null;
}


async function load() {

    const d = await fetch(
        '/api/data'
    ).then(x => x.json());

    document.getElementById('atmosphere-status').textContent = d.atmosphere.available
        ? d.atmosphere.source + ' · retrieved ' + localTime(d.atmosphere.retrieved_at_utc) +
          '. Nearest forecast sample: within ' +
          (d.atmosphere.nearest_tolerance_seconds === 10800 ? '3 hours' : '30 minutes') +
          '. Sample times appear below pressure values.'
        : 'Atmospheric forecast unavailable; tide and SSCOFS information remain available.';
    const r = d.risk;

    const riskElement =
        document.getElementById('risk');

    if (r.level === 'UNAVAILABLE') {
        riskElement.textContent =
            'Unavailable';

        riskElement.className =
            'big muted';

    } else {
        riskElement.textContent =
            r.level;

        if (r.level === 'HIGH') {
            riskElement.className =
                'big danger';

        } else if (
            r.level === 'ELEVATED' ||
            r.level === 'WATCH'
        ) {
            riskElement.className =
                'big warning';

        } else {
            riskElement.className =
                'big good';
        }
    }


    document.getElementById(
        'threshold'
    ).textContent =
        d.property_threshold_ft == null
        ? 'Not set'
        : d.property_threshold_ft
            .toFixed(1) + ' ft';


    const events =
        d.high_water_events || [];

    const eventTable =
        document.getElementById(
            'high-water-events'
        );

    const eventUnavailable =
        document.getElementById(
            'high-water-unavailable'
        );


    if (events.length === 0) {

        eventTable.innerHTML = '';

        eventUnavailable.style.display =
            'block';

    } else {

        eventUnavailable.style.display =
            'none';

        let closestIndex = -1;

        if (
            d.property_threshold_ft != null
        ) {
            let closestDistance =
                Infinity;

            events.forEach(
                (event, index) => {
                    if (event.sscofs_mllw_ft == null) return;

                    const distance =
                        Math.abs(
                            event.sscofs_mllw_ft -
                            d.property_threshold_ft
                        );

                    if (
                        distance <
                        closestDistance
                    ) {
                        closestDistance =
                            distance;

                        closestIndex =
                            index;
                    }
                }
            );
        }


        eventTable.innerHTML =
            '<tr>' +
            '<th>High-water period</th>' +
            '<th>Arcadia astronomical tide</th>' +
            '<th>Totten modeled total water</th>' +
            '<th>From threshold</th>' +
            '<th>Preceding 72-hr rain</th>' +
            '<th>Sea-level pressure</th><th>Wind</th>' +
            '</tr>' +

            events.map(
                (event, index) => {

                    const rowClass =
                        index === closestIndex
                        ? 'closest-event'
                        : '';

                    const levelClass =
                        marginClass(
                            event.sscofs_mllw_ft,
                            d.property_threshold_ft
                        );

                    return `
                        <tr class="${rowClass}">

                            <td>
                                <strong>
                                    ${localTime(
                                        event.event_time_utc
                                    )}
                                </strong><br>
                                <span class="muted">${event.kind === 'forecast' ? 'SSCOFS forecast' : 'Tide outlook'}</span>
                                ${event.king_tide ? '<br><span class="muted">KING TIDE</span>' : ''}
                            </td>

                            <td>
                                <strong>
                                    ${Number(
                                        event.arcadia_mllw_ft
                                    ).toFixed(2)}
                                    ft
                                </strong>
                                <br>
                                <span class="muted">
                                    ${tideTime(
                                        event.arcadia_time
                                    )}
                                </span>
                            </td>

                            <td>
                                <span
                                    class="
                                        event-level
                                        ${levelClass}
                                    "
                                >
                                    ${event.sscofs_mllw_ft == null ? 'Not available' : Number(event.sscofs_mllw_ft).toFixed(2) + ' ft'}
                                </span>
                            </td>

                            <td
                                class="
                                    event-level
                                    ${levelClass}
                                "
                            >
                                ${marginText(
                                    event.sscofs_mllw_ft,
                                    d.property_threshold_ft
                                )}
                            </td>

                            <td>
                                ${inches(
                                    event.preceding_72h_rain_in
                                )}
                            </td>
                            <td>${escapeText(event.pressure_label)}${event.pressure_mb == null ? '' : ' · ' + event.pressure_mb + ' mb'}<br>
                                <span class="muted note">${event.atmosphere_time_utc ? localTime(event.atmosphere_time_utc) : ''}</span></td>
                            <td>${escapeText(event.wind_text)}</td>

                        </tr>
                    `;
                }
            ).join('');
    }


    const observedRain =
        d.observed_rainfall;

    document.getElementById(
        'observed-rain'
    ).textContent =
        (
            observedRain &&
            observedRain.available
        )
        ? inches(
            observedRain.total_in
        )
        : 'Unavailable';


    const next72Rain =
        forecastRainNext72(
            d.rainfall
        );

    document.getElementById(
        'forecast-rain'
    ).textContent =
        next72Rain == null
        ? 'Unavailable'
        : inches(next72Rain);


    const rainfallRows =
        (
            d.rainfall &&
            d.rainfall.timeline
        )
        ? d.rainfall.timeline
        : [];


    document.getElementById(
        'weather'
    ).innerHTML =
        '<tr>' +
        '<th>Time</th>' +
        '<th>Wind</th>' +
        '<th>Rain chance</th>' +
        '<th>Preceding 72-hr rainfall</th>' +
        '<th>Forecast rain</th>' +
        '</tr>' +

        d.weather
        .slice(0,24)
        .map(
            (x, index) => {

                const rain =
                    rainfallRows[index];

                const rollingRain =
                    rain
                    ? inches(
                        rain.preceding_72h_rain_in
                    )
                    : '—';

                const forecastRain =
                    forecastIntervalText(
                        rain
                    );

                const chance =
                    x
                    .probabilityOfPrecipitation
                    ?.value;

                return `
                    <tr>

                        <td>
                            ${localTime(
                                x.startTime
                            )}
                        </td>

                        <td>
                            ${x.windDirection}
                            ${x.windSpeed}
                        </td>

                        <td>
                            ${
                                chance == null
                                ? '—'
                                : chance + '%'
                            }
                        </td>

                        <td>
                            ${rollingRain}
                        </td>

                        <td>
                            ${forecastRain}
                        </td>

                    </tr>
                `;
            }
        ).join('');


    const m = d.sscofs;
    document.getElementById('model-cycle').textContent =
        m.available ? localTime(m.cycle_utc) : 'Unavailable';


    if (!m.available) {

        document.getElementById(
            'model-unavailable'
        ).style.display =
            'block';

        document.getElementById(
            'model-data'
        ).style.display =
            'none';

        document.getElementById(
            'model-unavailable'
        ).textContent =
            'The NOAA total water-level ' +
            'forecast is temporarily ' +
            'unavailable. ' +
            (m.reason || '');

        return;
    }


    document.getElementById(
        'model-unavailable'
    ).style.display =
        'none';


    document.getElementById(
        'model-data'
    ).style.display =
        'block';


    document.getElementById(
        'model-peak'
    ).textContent =
        m.peak_mllw_ft.toFixed(2) +
        ' feet';


    document.getElementById(
        'model-time'
    ).textContent =
        localTime(
            m.peak_time_utc
        );


    document.getElementById(
        'technical-file'
    ).textContent =
        'NOAA forecast file: ' +
        m.file;


    const status =
        document.getElementById(
            'water-status'
        );


    const margin =
        document.getElementById(
            'model-margin'
        );


    if (m.margin_ft == null) {

        status.textContent =
            'No property flooding ' +
            'threshold has been ' +
            'configured.';

        status.className =
            'status-message';

        margin.textContent = '';

    } else if (
        m.margin_ft >= 0
    ) {

        status.textContent =
            'Forecast total water ' +
            'level exceeds your ' +
            'property flooding ' +
            'threshold.';

        status.className =
            'status-message danger';

        margin.textContent =
            m.margin_ft.toFixed(2) +
            ' feet above the threshold';

        margin.className =
            'margin-message danger';

    } else {

        const below =
            Math.abs(
                m.margin_ft
            );

        status.textContent =
            'Total water level is ' +
            'forecast to remain below ' +
            'your property flooding ' +
            'threshold.';

        margin.textContent =
            below.toFixed(2) +
            ' feet below the threshold';

        if (below <= 0.5) {

            status.className =
                'status-message warning';

            margin.className =
                'margin-message warning';

        } else {

            status.className =
                'status-message good';

            margin.className =
                'margin-message good';
        }
    }
}


load();

</script>

</body>
</html>
'''


@app.get(
    "/",
    response_class=HTMLResponse
)
async def home():
    return PAGE
