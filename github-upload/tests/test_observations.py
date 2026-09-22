import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from app.main import app


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {'OBSERVATIONS_DATABASE_URL':'postgresql://test/db','OBSERVATIONS_WRITE_KEY':'test-key'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.payload = {'id':str(uuid4()),'observed_at':'2026-09-22T10:15:00-07:00','reading_inches':'-18.5','note':'raw note'}
        self.headers = {'X-Observations-Key':'test-key'}

    def test_missing_configuration_fails_closed(self):
        with patch.dict(os.environ, {'OBSERVATIONS_DATABASE_URL':''}):
            self.assertEqual(self.client.get('/api/observations').status_code,503)
            self.assertEqual(self.client.post('/api/observations',json=self.payload,headers=self.headers).status_code,503)

    def test_auth(self):
        with patch('app.observations.connection') as connect:
            self.assertEqual(self.client.post('/api/observations',json=self.payload).status_code,401)
            connect.assert_not_called()

    def test_validation(self):
        for field, values in {'observed_at':['2026-09-22T10:00','2099-01-01T00:00:00Z',0,'bad'], 'reading_inches':[True,'NaN','Infinity',1201,-1201,'1.12345',None], 'note':['x'*2001,None], 'id':['bad']}.items():
            for value in values:
                with self.subTest(field=field,value=str(value)[:40]):
                    self.assertEqual(self.client.post('/api/observations',json={**self.payload,field:value},headers=self.headers).status_code,422)
        for limit in [0,101,'bad']:
            self.assertEqual(self.client.get('/api/observations',params={'limit':limit}).status_code,422)

    def test_db_failure_redacted(self):
        with patch('app.observations.connection',side_effect=psycopg.OperationalError('secret-password')):
            for response in [self.client.get('/api/observations'),self.client.post('/api/observations',json=self.payload,headers=self.headers)]:
                self.assertEqual(response.status_code,503)
                self.assertNotIn('secret-password',response.text)

    def row(self):
        return {**self.payload,'observed_at':datetime.fromisoformat(self.payload['observed_at']),'submitted_at':datetime.now(timezone.utc),'reading_inches':Decimal('-18.5')}

    def test_save_and_read(self):
        conn = MagicMock(); row=self.row()
        conn.execute.return_value.fetchone.return_value=row
        conn.execute.return_value.fetchall.return_value=[row]
        with patch('app.observations.connection') as connect:
            connect.return_value.__enter__.return_value=conn
            response=self.client.post('/api/observations',json=self.payload,headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()['saved'])
            self.assertIn('submitted_at',response.json()['observation'])
            self.assertEqual(self.client.get('/api/observations').json()['observations'][0]['note'],'raw note')
            connect.return_value.__exit__.assert_called()

    def test_retry_and_conflict(self):
        conn=MagicMock(); row=self.row()
        with patch('app.observations.connection') as connect:
            connect.return_value.__enter__.return_value=conn
            conn.execute.return_value.fetchone.side_effect=[None,row]
            self.assertEqual(self.client.post('/api/observations',json=self.payload,headers=self.headers).status_code,200)
            conn.execute.return_value.fetchone.side_effect=[None,{**row,'note':'different'}]
            self.assertEqual(self.client.post('/api/observations',json=self.payload,headers=self.headers).status_code,409)

    def test_commit_failure_never_reports_saved(self):
        with patch('app.observations.connection') as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=self.row()
            connect.return_value.__exit__.side_effect=psycopg.OperationalError('commit failed')
            self.assertEqual(self.client.post('/api/observations',json=self.payload,headers=self.headers).status_code,503)

    def test_collapsed_tile(self):
        page=self.client.get('/').text
        self.assertIn('<details id="local-observations">',page)
        self.assertIn('Local Tide Observations',page)

if __name__ == '__main__':
    unittest.main()
