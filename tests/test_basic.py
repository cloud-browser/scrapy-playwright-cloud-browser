from unittest.mock import MagicMock

import pytest
from scrapy.settings import Settings
from twisted.internet import asyncioreactor

from scrapy_cloud_browser import CloudBrowserHandler


asyncioreactor.install()


@pytest.fixture
def settings():
    yield {
        'CLOUD_BROWSER': {
            'PROXIES': ['socks5://someproxy'],
            'API_HOST': 'https://example.net',
            'API_TOKEN': 'some_token',
            'INIT_HANDLER': None,
            'PAGES_PER_BROWSER': 1,
            'PROXY_ORDERING': 'round-robin',
            'START_SEMAPHORES': 5,
        }
    }


def test_01_python_39(settings):
    crawler = MagicMock()
    crawler.settings = Settings(settings)
    assert crawler.settings.get('NUM_BROWSERS', 1) == 1
    oh = CloudBrowserHandler(crawler)
    assert isinstance(oh.options.host, str)
    assert oh.options.host == settings['CLOUD_BROWSER']['API_HOST'] + '/'
    assert isinstance(oh.options.token, str)


FAIL_VALUES = [
    ('API_HOST', ''),
    ('API_HOST', 'noturl'),
    ('PROXIES', []),
    ('PROXIES', ['noturl']),
    ('API_TOKEN', ''),
    ('PROXY_ORDERING', 'randomstring'),
]


@pytest.mark.parametrize('key, value', FAIL_VALUES)
def test_02_check_params(settings, key, value):
    crawler = MagicMock()
    settings['CLOUD_BROWSER'][key] = value
    crawler.settings = Settings(settings)

    with pytest.raises(ValueError):
        CloudBrowserHandler(crawler)
