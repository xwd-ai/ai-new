"""启动与本地故障分类应可在未加载模型 SDK 时执行。"""

from pathlib import Path
import subprocess
import sys


def test_services_and_health_do_not_import_sdk_but_provider_construction_does():
    result = subprocess.run([sys.executable, '-c', '''
import importlib.abc
import sys
import types

class RejectSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'openai' or fullname.startswith('openai.'):
            raise AssertionError('startup eagerly imported model SDK')

guard = RejectSdk()
sys.meta_path.insert(0, guard)
from backend.services import chat
from backend import providers
from backend.errors import classify_error, ModelConfigurationError
providers.capability_report()
from backend import database, seed
database.init_db = lambda: None
seed.seed_all = lambda: None
from backend.main import health
assert health().status_code == 200
for error, expected in [(TimeoutError(), 'timeout'), (ConnectionError(), 'connection_error'),
                        (ModelConfigurationError(), 'model_configuration'), (RuntimeError(), 'internal_error')]:
    assert classify_error(error).code == expected
assert 'openai' not in sys.modules
sys.meta_path.remove(guard)
calls = []
fake_sdk = types.ModuleType('openai')
def client_factory(**kwargs):
    calls.append(kwargs)
    return object()
fake_sdk.OpenAI = client_factory
sys.modules['openai'] = fake_sdk
for factory in [providers.OpenAICompatChatProvider, providers.OpenAICompatEmbeddingProvider,
                providers.OpenAICompatRerankProvider]:
    factory(api_key='test-key', base_url='https://example.invalid')
assert len(calls) == 3
assert all(call == {'api_key': 'test-key', 'base_url': 'https://example.invalid'} for call in calls)
print('lazy-sdk-ok')
'''], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'lazy-sdk-ok' in result.stdout
