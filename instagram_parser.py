"""Instagram: чтение видимой страницы через временный браузер, без API."""

from wb_parser import VisibleBrowserSource


class InstagramWebSource(VisibleBrowserSource):
    platform = 'Instagram'
    table = 'advertisers'
    hosts = ('www.instagram.com', 'instagram.com')
    path_pattern = '/[A-Za-z0-9_.]+/?'
