import asyncio
import itertools
import logging
import random
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union, Dict, Any

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
from scrapy.utils.reactor import verify_installed_reactor

from scrapy_playwright_cloud_browser.schemas import ProxyOrdering, SettingsScheme

logger = logging.getLogger(__name__)


@dataclass
class Options:
    host: str
    token: str
    timeout: int = 60
    init_handler: Optional[str] = None
    pages_per_browser: Optional[int] = None
    browser_settings: Optional[Dict[str, Any]] = None
    fingerprint: Optional[Dict[str, Any]] = None


class BrowserContextWrapperError(PlaywrightError):
    pass


class FakeSemaphore:
    async def release(self) -> None:
        pass

    async def acquire(self) -> None:
        pass


class ProxyManager:
    def __init__(
        self, proxies: Union[list[str], Callable[[], Awaitable[str]]], ordering: str
    ) -> None:
        ordering = ProxyOrdering(ordering)

        if isinstance(proxies, list):
            if not proxies:
                raise ValueError("Proxies list cannot be empty")

            if ordering == ProxyOrdering.ROUND_ROBIN:
                self._proxies = itertools.cycle(proxies)
            elif ordering == ProxyOrdering.RANDOM:
                self._proxies = proxies
            else:
                raise ValueError(f'Unknown ordering type: {ordering}')
        elif asyncio.iscoroutinefunction(proxies):
            self._proxies = proxies
        else:
            raise ValueError('Proxies must be a list or a coroutine function')

    async def get(self) -> str:
        if asyncio.iscoroutinefunction(self._proxies):
            return await self._proxies()
        elif isinstance(self._proxies, itertools.cycle):
            return str(next(self._proxies))
        else:
            return str(random.choice(self._proxies))


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

        logger.info(f'{options.init_handler=}')
        self._init_handler: Optional[Callable[[BrowserContext], Awaitable[None]]] = (
            self.load_init_handler(options.init_handler)
        )
        self._pages_per_browser_left: Optional[int] = None
        self._start_sem = start_sem
        self._proxy_manager = proxy_manager

        super().__init__()

    async def run(self):
        self._started = True
        logger.debug(f'{self.num}: RUN WORKER')
        self.start_heartbeat()
        while self._started:
            try:
                await self.connect()
                logger.debug(f'{self.num}: check connection')
                await self.check_connection()
                logger.debug(
                    f'{self.num}: put into queue with {self._browser_pool.qsize()} workers'
                )
                await self._browser_pool.put(self)
                # important: wait only if we put ourselves
                logger.debug(f'{self.num}: wait for next task')
                await self._wait.wait()
                self._wait.clear()
            except Exception:
                logger.exception(f'{self.num}: during worker loop')
                await self.close()
                continue

    def start_heartbeat(self) -> None:
        asyncio.create_task(self.heartbeat())

    async def heartbeat(self) -> None:
        cdp_session = None

        while self._started:
            logger.debug(f'{self.num}: Heartbeat: {self._last_ok_heartbeat}')

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
        logger.debug(f'{self.num}: connect')
        if self.is_established_connection():
            logger.debug(f'{self.num}: Established return')
            return

        if not self._playwright_context_manager:
            self._playwright_context_manager = PlaywrightContextManager()
        browser_type = await self.get_browser_type()
        proxy = await self._proxy_manager.get()
        ws_url = await self.get_ws_url(self._options, proxy)
        await asyncio.sleep(0.5)
        logger.debug(f'{self.num}: got ws: {ws_url}')
        with anyio.fail_after(10):
            browser: Browser = await browser_type.connect_over_cdp(
                endpoint_url=ws_url, timeout=10000
            )
        logger.debug(f'{self.num}: got browser: {browser}')
        await self.on_connect(browser)

    async def on_connect(self, browser: Browser) -> None:
        self.browser = browser
        self.context = await browser.new_context()
        logger.debug(f'{self.num}: got context: {self.context}')

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
            logger.debug(f'{self.num}: Response: {response.status=} {response=}')

        if not response or response.status > 499:
            await self.close()

        if self._options.pages_per_browser:
            self._pages_per_browser_left -= 1
            if self._pages_per_browser_left == 0:
                await self.close()

        self._wait.set()

    async def close(self):
        logger.warning(f'{self.num}: Close browser')
        if self.context:
            try:
                await self.context.close()
            except Exception:
                logger.exception(f'{self.num}: during context close')
            self.context = None

        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                logger.exception(f'{self.num}: during browser close')
            self.browser = None

        if self._playwright_instance:
            try:
                await self._playwright_instance.stop()
            except Exception:
                logger.exception(f'{self.num}: during playwright stop')
            self._playwright_instance = None

        if self._playwright_context_manager:
            try:
                await self._playwright_context_manager._connection.stop_async()
            except Exception:
                logger.exception(f'{self.num}: during playwright context manager stop')
            self._playwright_context_manager = None

    def is_established_connection(self) -> bool:
        if not isinstance(self.context, BrowserContext):
            return False

        return self.context.browser.is_connected()

    async def check_connection(self):
        if not self.context.browser.is_connected():
            raise BrowserContextWrapperError(f'{self.num}: Browser is not connected')

    async def get_ws_url(self, options: Options, proxy: str) -> str:
        async with httpx.AsyncClient(base_url=options.host) as client:
            async with self._start_sem:
                request_data = {'proxy': proxy}

                if options.browser_settings:
                    request_data['browser_settings'] = options.browser_settings

                if options.fingerprint:
                    request_data['fingerprint'] = options.fingerprint

                resp = await client.post(
                    '/profiles/one_time',
                    json=request_data,
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


CURRENT_WRAPPER: ContextVar[Optional[BrowserContextWrapper]] = ContextVar(
    'current_wrapper', default=None
)


class CloudBrowserHandler(ScrapyPlaywrightDownloadHandler):
    def __init__(self, crawler: Crawler) -> None:
        super().__init__(crawler)

        self.crawler = crawler
        self.settings = SettingsScheme(**self.crawler.settings.get('CLOUD_BROWSER', {}))

        self.num_browsers = self.settings.NUM_BROWSERS

        self.context_wrappers: FakeContextWrappers[str, BrowserContextWrapper] = (
            FakeContextWrappers()
        )

        self.options = Options(
            host=str(self.settings.API_HOST),
            token=self.settings.API_TOKEN,
            init_handler=self.settings.INIT_HANDLER,
            pages_per_browser=self.settings.PAGES_PER_BROWSER,
            browser_settings=self.settings.BROWSER_SETTINGS,
            fingerprint=self.settings.FINGERPRINT,
        )

        self.browser_pool = asyncio.Queue()
        self.workers = []

        self.start_sem = asyncio.Semaphore(self.settings.START_SEMAPHORES)
        self.proxy_manager = ProxyManager(self.settings.PROXIES, self.settings.PROXY_ORDERING)

    def start_workers(self):
        logger.debug('START WORKERS')

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
