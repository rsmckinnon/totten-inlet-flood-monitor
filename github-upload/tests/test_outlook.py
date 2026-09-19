import asyncio
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app import sources, outlook, risk, main

NOW = datetime(2026, 9, 19, 0, tzinfo=timezone.utc)
def tide(time):
    return {'t': time.astimezone(sources.PACIFIC).strftime('%Y-%m-%d %H:%M'), 'type':'H','v':'14.2'}
def model():
    return {'available':True, 'forecast_start_utc':NOW.isoformat(),
            'forecast_end_utc':(NOW+timedelta(hours=72)).isoformat(),
            'peaks':[{'time_utc':(NOW+timedelta(hours=h)).isoformat(), 'mllw_ft':15.4} for h in (12,36,60,72)]}

class OutlookTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache = patch.object(sources, 'CACHE_DIR', Path(directory.name))
        cache.start(); self.addCleanup(cache.stop)
        outlook._feed_cache = {}
        outlook._feed_lock = asyncio.Lock()

    def test_pressure_boundaries(self):
        for value, expected in [(984.9,'Extremely'),(985,'Very'),(994.9,'Very'),(995,'Low'),(1004.9,'Low'),(1005,'Normal')]:
            self.assertTrue(outlook.pressure_label(value).startswith(expected))
        for value in (None,'bad',float('nan'),float('inf')):
            self.assertIsNone(outlook.pressure_label(value))

    def test_horizon_and_no_extrapolation(self):
        times=[NOW+timedelta(hours=h) for h in (12,36,60,73,96,144,169)]
        rows=outlook.build_events([tide(t) for t in times],model(),{},now=NOW)
        self.assertEqual(len(rows),6)
        self.assertEqual([r['sscofs_mllw_ft'] for r in rows],[15.4]*3+[None]*3)
        self.assertEqual(rows[3]['kind'],'outlook')

    def test_missing_model_and_duplicates(self):
        t=tide(NOW+timedelta(days=1))
        rows=outlook.build_events([t,t],{'available':False},{},now=NOW)
        self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0]['sscofs_mllw_ft'])
        self.assertIsNone(rows[0]['pressure_mb'])

    def test_king_tide_only_metadata(self):
        t=tide(NOW+timedelta(hours=12))
        before=outlook.build_events([t],model(),{},NOW,king_dates=set())[0]
        after=outlook.build_events([t],model(),{},NOW,king_dates={t['t'][:10]})[0]
        self.assertFalse(before.pop('king_tide')); self.assertTrue(after.pop('king_tide'))
        self.assertEqual(before,after)
        self.assertEqual(risk.build_risk(before['sscofs_mllw_ft'],17.5),risk.build_risk(after['sscofs_mllw_ft'],17.5))

    def test_context_no_stale_reuse(self):
        atmosphere={'available':True,'hourly':[{'time_utc':NOW.isoformat(),'pressure_msl':989,'wind_speed_10m':22,'wind_direction_10m':180}]}
        result=outlook.context_at(NOW,atmosphere)
        self.assertEqual(result['pressure_mb'],989)
        self.assertEqual(result['wind_text'],'From S · 22.0 mph')
        self.assertIsNone(outlook.context_at(NOW+timedelta(hours=1),atmosphere)['pressure_mb'])

    def test_units_nulls_and_partial_arrays(self):
        p={'hourly_units':{'pressure_msl':'hPa','wind_speed_10m':'mp/h','wind_direction_10m':'°'},
           'hourly':{'time':[NOW.isoformat()],'pressure_msl':[None],'wind_speed_10m':[],'wind_direction_10m':[float('nan')]}}
        self.assertIsNone(outlook.parse_atmosphere(p)[0]['pressure_msl'])
        p['hourly_units']['pressure_msl']='Pa'
        with self.assertRaises(ValueError): outlook.parse_atmosphere(p)

    def test_provider_failure(self):
        with patch.object(sources,'get',AsyncMock(side_effect=sources.httpx.ConnectError('offline'))), patch.object(outlook,'fetch_metno',AsyncMock(side_effect=sources.httpx.ConnectError('offline'))):
            self.assertFalse(asyncio.run(outlook.atmospheric_forecast())['available'])

    def test_rainfall_outlook_time_and_missing_coverage(self):
        event={'event_time_utc':(NOW+timedelta(days=4)).isoformat(),'sscofs_time_utc':None}
        intervals=[{'start_utc':NOW.isoformat(),'end_utc':(NOW+timedelta(days=7)).isoformat(),'hourly_rate_in':0.1}]
        rows=sources.add_rainfall_to_high_water_events([event],{'forecast_intervals':intervals},{})
        self.assertIsNotNone(rows[0]['preceding_72h_rain_in'])
        self.assertTrue(outlook.check_rain_coverage(rows,{'forecast_intervals':intervals},{},NOW)[0]['rainfall_coverage_complete'])
        self.assertIsNone(outlook.check_rain_coverage(rows,{}, {},NOW)[0]['preceding_72h_rain_in'])

    def test_pacific_time(self):
        self.assertEqual(sources.parse_arcadia_time('2026-09-20 10:00').hour,17)
        self.assertEqual(sources.parse_arcadia_time('2026-12-20 10:00').hour,18)

    def test_api_outage_preserves_tides(self):
        future=datetime.now(timezone.utc)+timedelta(days=4)
        with patch.object(sources,'tides',AsyncMock(return_value=[tide(future)])), \
             patch.object(sources,'nws',AsyncMock(return_value=([],{}))), \
             patch.object(sources,'sscofs',AsyncMock(return_value={'available':False})), \
             patch.object(sources,'shelton_rainfall',AsyncMock(return_value={'available':False})), \
             patch.object(outlook,'atmospheric_forecast',AsyncMock(return_value={'available':False,'hourly':[]})):
            response=TestClient(main.app).get('/api/data')
        self.assertEqual(response.status_code,200)
        data=response.json()
        self.assertEqual(data['risk']['level'],'UNAVAILABLE')
        self.assertEqual(len(data['high_water_events']),1)
        self.assertIsNone(data['high_water_events'][0]['sscofs_mllw_ft'])
