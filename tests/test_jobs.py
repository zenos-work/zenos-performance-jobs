import os
import sys
import types
import unittest


class _DummyResponse:
    def __init__(self, body='', status=200, ok=True, data=None):
        self.status = status
        self.ok = ok
        self._body = body
        self._data = data or {}

    async def text(self):
        return self._body

    async def json(self):
        return self._data


workers_stub = types.ModuleType('workers')
workers_stub.WorkerEntrypoint = object
workers_stub.Response = _DummyResponse


async def _default_fetch(*_args, **_kwargs):
    return _DummyResponse(status=200, ok=True, data={})


workers_stub.fetch = _default_fetch
sys.modules['workers'] = workers_stub

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import index as jobs_index  # noqa: E402


class PlatformSnapshotJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_snapshot_collects_metrics(self):
        async def fake_fetch(url, method='GET', headers=None):
            if 'courses' in url:
                return _DummyResponse(status=200, ok=True, data={'courses': [1, 2]})
            if 'community' in url:
                return _DummyResponse(status=200, ok=True, data={'spaces': [1]})
            if 'marketplace' in url and 'connector-marketplace' not in url:
                return _DummyResponse(status=200, ok=True, data={'items': [1, 2, 3]})
            return _DummyResponse(status=200, ok=True, data={'listings': [1]})

        jobs_index.fetch = fake_fetch

        env = types.SimpleNamespace(API_BASE_URL='https://api.example.com', ZENOS_SERVICE_SECRET='token')
        result = await jobs_index._run_platform_snapshot_job(env, 'all')

        self.assertTrue(result['ok'])
        self.assertEqual(result['job'], 'platform-snapshot')
        self.assertEqual(result['details']['checks']['courses']['count'], 2)
        self.assertEqual(result['details']['checks']['community']['count'], 1)
        self.assertEqual(result['details']['checks']['marketplace']['count'], 3)
        self.assertEqual(result['details']['checks']['connectors']['count'], 1)


if __name__ == '__main__':
    unittest.main()
