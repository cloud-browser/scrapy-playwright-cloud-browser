from collections.abc import Awaitable, Callable
from typing import Union, Optional, Dict, Any
from enum import Enum

from pydantic import AnyUrl, BaseModel, ConfigDict, HttpUrl, PositiveInt, conlist, constr


class ProxyOrdering(Enum):
    RANDOM = 'random'
    ROUND_ROBIN = 'round-robin'


class SettingsScheme(BaseModel):
    API_HOST: HttpUrl
    API_TOKEN: constr(min_length=1)
    NUM_BROWSERS: Optional[PositiveInt] = 1
    PROXIES: conlist(item_type=Union[AnyUrl, Callable[[None], Awaitable[AnyUrl]]], min_length=1)
    INIT_HANDLER: Optional[str] = None
    PAGES_PER_BROWSER: Optional[PositiveInt] = 100
    START_SEMAPHORES: Optional[PositiveInt] = 10
    PROXY_ORDERING: Optional[ProxyOrdering] = ProxyOrdering.RANDOM
    BROWSER_SETTINGS: Optional[Dict[str, Any]] = None
    FINGERPRINT: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(title='CLOUD_BROWSER')
