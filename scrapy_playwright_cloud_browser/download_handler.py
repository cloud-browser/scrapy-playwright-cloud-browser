import asyncio
import itertools
import logging
import random
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

import anyio
import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    BrowserType,
    CDPSession,
    Error as PlaywrightError,
    PlaywrightContextManager,
)
from playwright.async_api._generated import Playwright as AsyncPlaywright
from scrapy import Spider
from scrapy.crawler import Crawler
from scrapy.http import Request, Response
from scrapy.utils.defer import deferred_from_coro
from scrapy.utils.misc import load_object
from scrapy_playwright.handler import DEFAULT_BROWSER_TYPE, ScrapyPlaywrightDownloadHandler
from twisted.internet.defer import Deferred

from scrapy_playwright_cloud_browser.settings import SettingsScheme

log = logging.getLogger(__name__)


@dataclass
class Options:
    host: str
    token: str
    timeout: int = 60
    init_handler: Optional[str] = None
    pages_per_browser: Optional[int] = None


class BrowserContextWrapperError(PlaywrightError):
    pass


class FakeSemaphore:
    async def release(self) -> None:
        pass

    async def acquire(self) -> None:
        pass


class ProxyManager:
    def __init__(
        self, proxies: Union[list[str], Callable[[None], Awaitable[str]]], ordering: str
    ) -> None:
        assert ordering in ('random', 'round-robin')

        if isinstance(proxies, list):
            if ordering == 'round-robin':
                self.proxies = itertools.cycle(proxies)
            else:
                self.proxies = proxies
        elif asyncio.iscoroutinefunction(proxies):
            self.proxies = proxies
        else:
            raise ValueError('Proxies must be list or coroutine function')

    async def get(self) -> str:
        if asyncio.iscoroutinefunction(self.proxies):
            return await self.proxies()

        if isinstance(self.proxies, itertools.cycle):
            return str(next(self.proxies))

        return str(random.choice(self.proxies))


class BrowserContextWrapper:
    def __init__(
        self,
        num: int,
        browser_pool: asyncio.Queue,
        options: Options,
        start_sem: asyncio.Semaphore,
        proxy_manager: ProxyManager,
    ) -> None:
        self.num = num
        self.semaphore = FakeSemaphore()
        self.context = None

        self._browser_pool = browser_pool
        self._options = options
        self._started = False
        self._playwright_context_manager = None
        self._playwright_instance: Optional[AsyncPlaywright] = None
        self._wait = asyncio.Event()
        self.browser: Optional[Browser] = None
        self._last_ok_heartbeat = False
        self._heartbeat_interval = 5

        log.info(f'{options.init_handler=}')
        self._init_handler: Optional[
            Callable[[BrowserContext], Awaitable[None]]
        ] = self.load_init_handler(options.init_handler)
        self._pages_per_browser_left: Optional[int] = None
        self._start_sem = start_sem
        self._proxy_manager = proxy_manager

        super().__init__()

    async def run(self):
        self._started = True
        log.info(f'{self.num}: RUN WORKER')
        self.start_heartbeat()
        while self._started:
            try:
                await self.connect()
                log.info(f'{self.num}: check connection')
                await self.check_connection()
                log.info(f'{self.num}: put into queue with {self._browser_pool.qsize()} workers')
                await self._browser_pool.put(self)
                # important: wait only if we put ourselves
                log.info(f'{self.num}: wait for next task')
                await self._wait.wait()
                self._wait.clear()
            except Exception:
                log.exception(f'{self.num}: during worker loop')
                await self.close()
                continue

    def start_heartbeat(self) -> None:
        asyncio.create_task(self.heartbeat())

    async def heartbeat(self) -> None:
        cdp_session = None

        while self._started:
            log.info(f'{self.num}: Heartbeat: {self._last_ok_heartbeat}')

            if self.context:
                if not isinstance(cdp_session, CDPSession):
                    cdp_session = await self.context.browser.new_browser_cdp_session()

                resp = await cdp_session.send('SystemInfo.getProcessInfo')
                success_proc_info = resp.get('processInfo') is not None
                is_connected = self.context.browser.is_connected()
                self._last_ok_heartbeat = is_connected and success_proc_info
            else:
                self._last_ok_heartbeat = False

            await asyncio.sleep(self._heartbeat_interval)

    async def get_browser_type(self) -> BrowserType:
        if not self._playwright_instance:
            self._playwright_instance = await self._playwright_context_manager.start()
        browser_type: BrowserType = getattr(self._playwright_instance, DEFAULT_BROWSER_TYPE)
        return browser_type

    async def connect(self) -> None:
        log.info(f'{self.num}: connect')
        if self.is_established_connection():
            log.info(f'{self.num}: Established return')
            return

        if not self._playwright_context_manager:
            self._playwright_context_manager = PlaywrightContextManager()
        browser_type = await self.get_browser_type()
        proxy = await self._proxy_manager.get()
        ws_url = await self.get_ws_url(self._options, proxy)
        await asyncio.sleep(0.5)
        log.info(f'{self.num}: got ws: {ws_url}')
        with anyio.fail_after(10):
            browser: Browser = await browser_type.connect_over_cdp(
                endpoint_url=ws_url, timeout=10000
            )
        log.info(f'{self.num}: got browser: {browser}')
        await self.on_connect(browser)

    async def on_connect(self, browser: Browser) -> None:
        self.browser = browser
        self.context = await browser.new_context()
        log.info(f'{self.num}: got context: {self.context}')

        self._last_ok_heartbeat = True

        if self._init_handler:
            await self._init_handler(self.context)  # noqa

        if self._options.pages_per_browser:
            self._pages_per_browser_left = self._options.pages_per_browser

    def load_init_handler(
        self, path: Optional[str]
    ) -> Optional[Callable[[BrowserContext], Awaitable[None]]]:
        if not path:
            return
        handler = load_object(path)
        assert asyncio.iscoroutinefunction(handler)
        return handler

    async def on_response(self, response: Optional[Response]):
        if response:
            log.info(f'{self.num}: Response: {response.status=} {response=}')

        if not response or response.status > 499:
            await self.close()

        if self._options.pages_per_browser:
            self._pages_per_browser_left -= 1
            if self._pages_per_browser_left == 0:
                await self.close()

        self._wait.set()

    async def close(self):
        log.warning(f'{self.num}: Close browser')
        if self.context:
            try:
                await self.context.close()
            except Exception:
                log.exception(f'{self.num}: during context close')
            self.context = None

        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                log.exception(f'{self.num}: during browser close')
            self.browser = None

        if self._playwright_instance:
            try:
                await self._playwright_instance.stop()
            except Exception:
                log.exception(f'{self.num}: during playwright stop')
            self._playwright_instance = None

        if self._playwright_context_manager:
            try:
                await self._playwright_context_manager._connection.stop_async()
            except Exception:
                log.exception(f'{self.num}: during playwright context manager stop')
            self._playwright_context_manager = None

    def is_established_connection(self) -> bool:
        if not isinstance(self.context, BrowserContext):
            return False

        return self.context.browser.is_connected()

    async def check_connection(self):
        # TODO: add necessary checks here
        if not self.context.browser.is_connected():
            raise BrowserContextWrapperError(f'{self.num}: Browser is not connected')

    async def get_ws_url(self, options: Options, proxy: str) -> str:
        async with httpx.AsyncClient() as client:
            async with self._start_sem:
                resp = await client.post(
                    f'{options.host}profiles/one_time',
                    json={'proxy': proxy, 'browser_settings': {'inactive_kill_timeout': 15}},
                    headers={'x-cloud-api-token': options.token},
                    timeout=options.timeout,
                )
                resp.raise_for_status()
                return resp.json()['ws_url']


class FakeContextWrappers(dict):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def __getitem__(self, key: str) -> None:
        pass

    def __setitem__(self, key: str, value: BrowserContextWrapper) -> None:
        pass

    def __len__(self):
        pass

    def get(self, key: str) -> None:
        pass

    def values(self) -> list:
        return []


CURRENT_WRAPPER: ContextVar[Optional[BrowserContextWrapper]] = ContextVar('current_wrapper', default=None)


class CloudBrowserHandler(ScrapyPlaywrightDownloadHandler):
    def __init__(self, crawler: Crawler) -> None:
        super().__init__(crawler)

        self.crawler = crawler
        self.settings = SettingsScheme(**self.crawler.settings.get('CLOUD_BROWSER', {}))

        self.num_browsers = self.settings.NUM_BROWSERS

        self.context_wrappers: FakeContextWrappers[
            str, BrowserContextWrapper
        ] = FakeContextWrappers()

        proxies = self.settings.PROXIES
        host = str(self.settings.API_HOST)
        if not host.endswith('/'):
            host += '/'
        self.options = Options(
            host=host,
            token=self.settings.API_TOKEN,
            init_handler=self.settings.INIT_HANDLER,
            pages_per_browser=self.settings.PAGES_PER_BROWSER,
        )

        self.browser_pool = asyncio.Queue()
        self.workers = []

        self.start_sem = asyncio.Semaphore(self.settings.START_SEMAPHORES)
        self.proxy_manager = ProxyManager(proxies, self.settings.PROXY_ORDERING)

    def start_workers(self):
        log.info('START WORKERS')

        for i in range(self.num_browsers):
            self.workers.append(
                asyncio.create_task(
                    BrowserContextWrapper(
                        i, self.browser_pool, self.options, self.start_sem, self.proxy_manager
                    ).run()
                )
            )

    async def get_browser(self) -> BrowserContextWrapper:
        while True:
            browser = await self.browser_pool.get()
            if browser._last_ok_heartbeat:
                return browser
            await browser.on_response(None)
            log.info('Browser is not ready, try another one')
            await asyncio.sleep(0)

    async def _download_request(self, request: Request, spider: Spider) -> Response:
        if not self.workers:
            self.start_workers()
        browser = await self.get_browser()
        CURRENT_WRAPPER.set(browser)
        response = None
        try:
            response = await super()._download_request(request, spider)
            return response
        finally:
            await browser.on_response(response)

    def download_request(self, request: Request, spider: Spider) -> Deferred:
        log.info('download_request %s', request)
        return deferred_from_coro(self._download_request(request, spider))

    async def _create_browser_context(
        self,
        name: str,
        context_kwargs: Optional[dict],  # noqa
        spider: Optional[Spider] = None,  # noqa
    ) -> BrowserContextWrapper:
        browser = CURRENT_WRAPPER.get()
        assert browser
        return browser
