import os
from dotenv import load_dotenv
load_dotenv()

def f(name, default=None):
    v=os.getenv(name,"").strip()
    return float(v) if v else default

PROPERTY_LAT=f("PROPERTY_LAT",47.1)
PROPERTY_LON=f("PROPERTY_LON",-123.0)
PROPERTY_NAME=os.getenv("PROPERTY_NAME","Oyster Bay, Totten Inlet")
PROPERTY_FLOOD_THRESHOLD_FT=f("PROPERTY_FLOOD_THRESHOLD_FT")
FLOOD_ALERT_MARGIN_FT=f("FLOOD_ALERT_MARGIN_FT",0.3)
NWS_USER_AGENT=os.getenv("NWS_USER_AGENT","TottenInletFloodMonitor/1.0 (contact@example.com)")
COOPS_STATION=os.getenv("COOPS_STATION","9446666")
COOPS_UNITS=os.getenv("COOPS_UNITS","english")
COOPS_DATUM=os.getenv("COOPS_DATUM","MLLW")
COOPS_TIMEZONE=os.getenv("COOPS_TIMEZONE","LST_LDT")
RAINFALL_GRID_FIELD=os.getenv("RAINFALL_GRID_FIELD","quantitativePrecipitation")
SSCOFS_ENDPOINT=os.getenv("SSCOFS_ENDPOINT","").strip()
# SSCOFS Totten Inlet Entrance
SSCOFS_STATION_INDEX = 93

# NOAA VDatum: MLLW minus MSL at the Totten SSCOFS station
SSCOFS_MLLW_TO_MSL_M = -2.526926

# Operational SSCOFS forecast cycle.
# We'll automatically locate the newest available station forecast.
SSCOFS_NOMADS_BASE = (
    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nosofs/prod"
)
