import logging

from scrapy.crawler import Crawler
from scrapy.exceptions import UsageError
from scrapy.utils.misc import load_object

from pydantic import ValidationError

from scrapy_playwright_cloud_browser.download_handler import CloudBrowserHandler
from scrapy_playwright_cloud_browser.schemas import SettingsScheme

logger = logging.getLogger(__name__)


class CloudBrowserExtension:
    @classmethod
    def from_crawler(cls, crawler: Crawler):
        logger.info("Initialized cloud browser extension")

        try:
            SettingsScheme(**crawler.settings.get("CLOUD_BROWSER", {}))
        except ValidationError as e:
            raise UsageError(str(e))
        except:
            raise UsageError("Error during instantiate cloud browser extension")

        download_handlers = crawler.settings.get("DOWNLOAD_HANDLERS")
        for handler_type in ["http", "https"]:
            handler_path = download_handlers.get(handler_type)
            if handler_path is None:
                download_handlers[handler_type] = (
                    "scrapy_playwright_cloud_browser.CloudBrowserHandler"
                )
                logger.info(
                    "%s handler doesn't set explicitly, set to default cloud browser handler",
                    handler_type,
                )
            else:
                handler = load_object(handler_path)
                if not issubclass(handler, CloudBrowserHandler):
                    raise UsageError(
                        f"{handler_type} download handler has to be inherited from cloud browser download handler"
                    )

        return cls()
