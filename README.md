# scrapy-cloud-browser

[![PyPI - Version](https://img.shields.io/pypi/v/scrapy-cloud-browser.svg)](https://pypi.org/project/scrapy-cloud-browser)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/scrapy-cloud-browser.svg)](https://pypi.org/project/scrapy-cloud-browser)

-----

## Installation

```console
pip install scrapy-cloud-browser
```

## Usage

Setup environment variables in `settings.py` in `CLOUD_BROWSER` namespace:

```console
CLOUD_BROWSER = {
    "API_HOST": <HOST>,
    "API_TOKEN": <API_TOKEN>,
    "NUM_BROWSERS": <NUM_BROWSERS>,
    "PROXIES" = [<proxy>],
    "INIT_HANDLER": <INIT_HANDLER>,
    "PAGES_PER_BROWSER": <PAGES_PER_BROWSER>,
    "START_SEMAPHORES": <START_SEMAPHORES>,
    "PROXY_ORDERING": <PROXY_ORDERING>
}
```

Add cloud browser handlers and change reactor in `settings.py`:

```python
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

EXTENSIONS = {
    'scrapy_cloud_browser.CloudBrowserExtension': 500,
}
```


## License

`scrapy-cloud-browser` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
