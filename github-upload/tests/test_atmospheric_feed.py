import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from app import outlook, sources


def response(payload=None, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {},
                          request=httpx.Request('GET', 'https://example.test/forecast'))


def met_payload():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return {'properties': {'meta': {'updated_at': now.isoformat(), 'units': {
        'air_pressure_at_sea_level': 'hPa', 'wind_speed': 'm/s', 'wind_from_direction': 'degrees'}},
        'timeseries': [{'time': (now + timedelta(hours=h)).isoformat(), 'data': {'instant': {
            'details': {'air_pressure_at_sea_level': 989, 'wind_speed': 10, 'wind_from_direction': 180}}}}
            for h in range(0, 193, 6)]}}


class FeedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(sources, 'CACHE_DIR', Path(self.directory.name))
        patcher.start(); self.addCleanup(patcher.stop)
        outlook._feed_cache = {}
        outlook._feed_lock = asyncio.Lock()

    async def test_429_fallback_and_cache_across_visitors_and_restart(self):
        rejected = response(status=429, headers={'Retry-After': '7200'})
        error = httpx.HTTPStatusError('limited', request=rejected.request, response=rejected)
        with patch.object(sources, 'get', AsyncMock(side_effect=error)) as primary, \
             patch.object(outlook, 'fetch_metno', AsyncMock(return_value=response(met_payload()))) as fallback:
            first = await outlook.atmospheric_forecast()
            self.assertTrue(first['available'])
            self.assertIn('MET Norway', first['source'])
            self.assertAlmostEqual(first['hourly'][0]['wind_speed_10m'],22.36936,places=4)
            again = await asyncio.gather(*[outlook.atmospheric_forecast() for _ in range(5)])
            self.assertEqual(primary.await_count, 1); self.assertEqual(fallback.await_count, 1)
            outlook._feed_cache = {}  # Restart reloads persistent cooldown and forecast.
            self.assertEqual((await outlook.atmospheric_forecast())['hourly'], first['hourly'])
            self.assertEqual(primary.await_count, 1); self.assertEqual(fallback.await_count, 1)
            key = next(iter(outlook._feed_cache))
            self.assertGreater(outlook._feed_cache[key]['open_meteo']['retry_at'], datetime.now(timezone.utc).timestamp()+7100)

    async def test_both_fail_and_cooldown(self):
        with patch.object(sources, 'get', AsyncMock(side_effect=httpx.ConnectError('offline'))) as primary, \
             patch.object(outlook, 'fetch_metno', AsyncMock(side_effect=httpx.ConnectError('offline'))) as fallback:
            self.assertFalse((await outlook.atmospheric_forecast())['available'])
            self.assertFalse((await outlook.atmospheric_forecast())['available'])
            self.assertEqual(primary.await_count, 1); self.assertEqual(fallback.await_count, 1)

    async def test_primary_success_no_fallback(self):
        rows=met_payload()['properties']['timeseries']
        payload={'hourly_units':{'pressure_msl':'hPa','wind_speed_10m':'mp/h','wind_direction_10m':'°'},
                 'hourly':{'time':[r['time'] for r in rows], 'pressure_msl':[1010]*len(rows),
                           'wind_speed_10m':[4]*len(rows),'wind_direction_10m':[180]*len(rows)}}
        with patch.object(sources, 'get', AsyncMock(return_value=response(payload))), \
             patch.object(outlook, 'fetch_metno', AsyncMock()) as fallback:
            self.assertIn('Open-Meteo',(await outlook.atmospheric_forecast())['source'])
            fallback.assert_not_awaited()

    async def test_expiry_and_conditional_304(self):
        expires=datetime.now(timezone.utc)+timedelta(hours=2)
        modified=format_datetime(datetime.now(timezone.utc),usegmt=True)
        first=response(met_payload(),headers={'Expires':format_datetime(expires,usegmt=True),'Last-Modified':modified})
        with patch.object(sources, 'get', AsyncMock(side_effect=httpx.ConnectError('offline'))), \
             patch.object(outlook, 'fetch_metno', AsyncMock(side_effect=[first,response(status=304)])) as fallback:
            before=await outlook.atmospheric_forecast()
            key=next(iter(outlook._feed_cache))
            self.assertGreaterEqual(outlook._feed_cache[key]['met_norway']['retry_at'],expires.timestamp()-1)
            outlook._feed_cache[key]['met_norway']['retry_at']=0
            after=await outlook.atmospheric_forecast()
            self.assertEqual(after['hourly'], before['hourly'])
            self.assertEqual(after['retrieved_at_utc'], before['retrieved_at_utc'])
            self.assertEqual(fallback.call_args.args[1],modified)

    async def test_expired_cache_not_served_on_outage(self):
        with patch.object(sources, 'get', AsyncMock(side_effect=httpx.ConnectError('offline'))), \
             patch.object(outlook, 'fetch_metno', AsyncMock(side_effect=[response(met_payload()),httpx.ConnectError('offline')])):
            await outlook.atmospheric_forecast()
            key=next(iter(outlook._feed_cache))
            outlook._feed_cache[key]['met_norway']['retry_at']=0
            self.assertFalse((await outlook.atmospheric_forecast())['available'])
            self.assertFalse((await outlook.atmospheric_forecast())['available'])

    def test_met_units_missing_values_and_matching(self):
        payload=met_payload()
        rows=outlook.parse_metno(payload)
        atmosphere={'available':True,'hourly':rows,'nearest_tolerance_seconds':10800}
        time=outlook.utc(rows[0]['time_utc'])+timedelta(hours=3)
        result=outlook.context_at(time,atmosphere)
        self.assertEqual(result['pressure_mb'],989)
        self.assertEqual(result['atmosphere_time_utc'],rows[0]['time_utc'])
        self.assertIsNone(outlook.context_at(outlook.utc(rows[-1]['time_utc'])+timedelta(minutes=1),atmosphere)['pressure_mb'])
        payload['properties']['timeseries'][0]['data']['instant']['details']={}
        self.assertIsNone(outlook.parse_metno(payload)[0]['pressure_msl'])
        payload['properties']['meta']['units']['wind_speed']='knots'
        with self.assertRaises(ValueError):outlook.parse_metno(payload)
