import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
import netCDF4
from app import sources


class SSCOFSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / 'sscofs.t03z.20990101.stations.forecast.nc'
        self.cycle = datetime(2099, 1, 1, 3, tzinfo=timezone.utc)
        self.file = dict(path=self.path, filename=self.path.name, cycle=self.cycle)
        with netCDF4.Dataset(self.path, 'w') as ds:
            ds.createDimension('time', 5)
            ds.createDimension('station', 94)
            t = ds.createVariable('time', 'f8', ('time',))
            t.units = 'hours since 2099-01-01 00:00:00'
            t[:] = [0, 6, 12, 18, 24]
            z = ds.createVariable('zeta', 'f4', ('time', 'station'), fill_value=-9999)
            z[:] = 500
            z[:, 93] = [0, 1, 0, 2, 0]
        sources._sscofs_cache.clear()
        for obj, name, value in [(sources, 'CACHE_DIR', self.directory),
                                 (sources, '_sscofs_lock', asyncio.Lock()),
                                 (sources.config, 'SSCOFS_ENDPOINT', '')]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def assert_plain(self, value):
        self.assertIn(type(value), (dict, list, tuple, str, int, float, bool, type(None)))
        if type(value) is dict:
            for k, v in value.items():
                self.assert_plain(k)
                self.assert_plain(v)
        elif type(value) in (list, tuple):
            for item in value:
                self.assert_plain(item)

    async def test_concurrent_refresh_and_isolation(self):
        async def download():
            await asyncio.sleep(0)
            return self.file
        with patch.object(sources, 'download_sscofs_file', AsyncMock(side_effect=download)) as fetch, patch.object(sources, '_process_sscofs_file', wraps=sources._process_sscofs_file) as parse:
            results = await asyncio.gather(*(sources.sscofs() for _ in range(20)))
            self.assertTrue(all(r['available'] for r in results))
            self.assertEqual(fetch.await_count, 1)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(results[0]['peak_mllw_ft'], round((2 + 2.526926) * 3.28084, 2))
            results[0]['peaks'].clear()
            self.assertTrue((await sources.sscofs())['peaks'])
            self.assert_plain(sources._sscofs_cache)
            json.dumps(results[1], allow_nan=False)

    async def test_ttl_same_cycle_then_new_cycle(self):
        with patch.object(sources, 'download_sscofs_file', AsyncMock(return_value=self.file)) as fetch, patch.object(sources, '_process_sscofs_file', wraps=sources._process_sscofs_file) as parse, patch.object(sources.time, 'monotonic', return_value=100) as clock:
            await sources.sscofs()
            self.assertEqual(sources._sscofs_cache['expires_at'], 1900)
            clock.return_value = 1901
            await sources.sscofs()
            self.assertEqual(fetch.await_count, 2)
            self.assertEqual(parse.call_count, 1)
            new = self.directory / 'sscofs.t09z.20990101.stations.forecast.nc'
            new.write_bytes(self.path.read_bytes())
            fetch.return_value = dict(path=new, filename=new.name, cycle=self.cycle.replace(hour=9))
            clock.return_value = 3702
            self.assertEqual((await sources.sscofs())['file'], new.name)
            self.assertEqual(parse.call_count, 2)

    async def test_failure_backoff_and_recovery(self):
        with patch.object(sources, 'download_sscofs_file', AsyncMock(side_effect=OSError('offline'))) as fetch:
            results = await asyncio.gather(*(sources.sscofs() for _ in range(10)))
            self.assertTrue(all(not r['available'] for r in results))
            self.assertEqual(fetch.await_count, 1)
            sources._sscofs_cache['expires_at'] = 0
            fetch.side_effect = None
            fetch.return_value = self.file
            self.assertTrue((await sources.sscofs())['available'])

    async def test_config_change(self):
        with patch.object(sources, 'download_sscofs_file', AsyncMock(return_value=self.file)):
            await sources.sscofs()
            with patch.object(sources.config, 'PROPERTY_FLOOD_THRESHOLD_FT', 17.5):
                result = await sources.sscofs()
                self.assertEqual(result['threshold_ft'], 17.5)
                self.assertEqual(result['margin_ft'], round((2 + 2.526926) * 3.28084 - 17.5, 2))

    async def test_dataset_closes_on_success_and_error(self):
        real = netCDF4.Dataset
        opened = []
        def track(*args, **kwargs):
            ds = real(*args, **kwargs)
            opened.append(ds)
            return ds
        with patch.object(sources.netCDF4, 'Dataset', side_effect=track):
            sources._process_sscofs_file(self.file)
            self.assertFalse(opened[-1].isopen())
            with patch.object(sources.netCDF4, 'num2date', side_effect=ValueError('bad time')):
                with self.assertRaises(ValueError):
                    sources._process_sscofs_file(self.file)
            self.assertFalse(opened[-1].isopen())

    async def test_masked_nonfinite_values(self):
        with netCDF4.Dataset(self.path, 'a') as ds:
            ds['zeta'][:, 93] = [0, -9999, float('nan'), float('inf'), 1]
        result = sources._process_sscofs_file(self.file)
        self.assertEqual(result['peak_mllw_ft'], round((1 + 2.526926) * 3.28084, 2))
        self.assert_plain(result)
        json.dumps(result, allow_nan=False)

    async def test_corrupt_file_retry(self):
        self.path.write_bytes(b'broken')
        with patch.object(sources, 'download_sscofs_file', AsyncMock(return_value=self.file)):
            self.assertFalse((await sources.sscofs())['available'])
        self.assertFalse(self.path.exists())

    async def test_expired_forecast(self):
        with netCDF4.Dataset(self.path, 'a') as ds:
            ds['time'].units = 'hours since 2000-01-01 00:00:00'
        with patch.object(sources, 'download_sscofs_file', AsyncMock(return_value=self.file)):
            result = await sources.sscofs()
        self.assertFalse(result['available'])
        self.assertIn('expired', result['reason'])
        self.assertTrue(self.path.exists())

    async def test_cleanup_scope(self):
        names = ['sscofs.t03z.20981231.stations.forecast.nc',
                 'sscofs.t09z.20981231.stations.forecast.nc',
                 'sscofs.t21z.20981231.stations.forecast.nc',
                 'sscofs.t09z.20990101.stations.forecast.nc',
                 'sscofs.t03z.20981231.stations.nowcast.nc', 'atmosphere.json', 'research.nc']
        for name in names:
            (self.directory / name).touch()
        sources._cleanup_sscofs_files(self.file)
        self.assertFalse(any((self.directory / n).exists() for n in names[:2]))
        self.assertTrue(all((self.directory / n).exists() for n in names[2:]))
        self.assertTrue(self.path.exists())

    async def test_disk_cache(self):
        candidate = dict(self.file, url='https://example.test/forecast.nc')
        with patch.object(sources, 'sscofs_candidate_cycles', return_value=[candidate]), patch.object(sources, '_download_sscofs_to_path', AsyncMock()) as download:
            self.assertEqual(await sources.download_sscofs_file(), self.file)
            download.assert_not_awaited()

    async def test_atomic_stream_and_failure_cleanup(self):
        client_type = httpx.AsyncClient
        destination = self.directory / 'download.nc'
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b'x' * 200000))
        with patch.object(sources.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=transport, **kw)):
            await sources._download_sscofs_to_path('https://example.test/test.nc', destination)
        self.assertEqual(destination.stat().st_size, 200000)
        self.assertFalse(list(self.directory.glob('*.part')))
        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'partial'
                raise httpx.ReadError('interrupted')
        transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=BrokenStream()))
        with patch.object(sources.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=transport, **kw)):
            with self.assertRaises(httpx.ReadError):
                await sources._download_sscofs_to_path('https://example.test/test.nc', destination)
        self.assertEqual(destination.stat().st_size, 200000)
        self.assertFalse(list(self.directory.glob('*.part')))

    async def test_cancel_releases_lock(self):
        async def wait():
            await asyncio.Event().wait()
        with patch.object(sources, 'download_sscofs_file', AsyncMock(side_effect=wait)):
            task = asyncio.create_task(sources.sscofs())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch.object(sources, 'download_sscofs_file', AsyncMock(return_value=self.file)):
            self.assertTrue((await sources.sscofs())['available'])
