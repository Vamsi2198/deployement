import os
import tempfile
from unittest.mock import Mock

from deploy_engine import detect_stack, deploy_to_render


def test_detect_stack_prefers_main_fastapi_app():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, 'deploy_engine.py'), 'w', encoding='utf-8') as f:
            f.write('from fastapi import FastAPI\n# false positive used by different deployment logic\nFastAPI()\n')
        with open(os.path.join(d, 'main.py'), 'w', encoding='utf-8') as f:
            f.write('from fastapi import FastAPI\napp = FastAPI()\n')

        build_command, start_command = detect_stack(d, {'logs': []})

        assert build_command == 'pip install -r requirements.txt'
        assert start_command == 'uvicorn main:app --host 0.0.0.0 --port $PORT'


def test_deploy_to_render_accepts_202_accepted(monkeypatch):
    def fake_get(url, headers=None, params=None):
        class RespServices:
            status_code = 200
            def json(self):
                return [{"service": {"id": "svc_123", "name": "demo-service"}}]

        class RespDeploys:
            status_code = 200
            def json(self):
                return [{"deploy": {"status": "live"}}]

        class RespService:
            status_code = 200
            def json(self):
                return {"service": {"serviceDetails": {"url": "https://example.com"}}}

        if '/services' in url and 'deploys' not in url and url.endswith('/services'):
            return RespServices()
        if '/deploys' in url:
            return RespDeploys()
        if '/services/' in url:
            return RespService()
        return RespService()

    class RespPatch:
        status_code = 200
        text = "ok"

    class RespDeploy:
        status_code = 202
        text = "accepted"

    monkeypatch.setattr('requests.patch', lambda *a, **k: RespPatch())
    monkeypatch.setattr('requests.post', lambda *a, **k: RespDeploy() if 'deploys' in a[0] else Mock(status_code=200, json=lambda: {'service': {'id': 'svc_123'}}))
    monkeypatch.setattr('requests.get', fake_get)

    result = deploy_to_render(
        'https://github.com/test/repo',
        'demo-service',
        'token123',
        'pip install -r requirements.txt',
        'uvicorn main:app --host 0.0.0.0 --port $PORT',
        {'logs': []},
        overwrite=True,
    )

    assert result == 'https://example.com'
