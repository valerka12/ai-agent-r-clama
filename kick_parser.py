"""Kick: чтение видимой страницы через временный браузер, без API."""

from wb_parser import VisibleBrowserSource


class KickWebSource(VisibleBrowserSource):
    platform = 'Kick'
    table = 'streamers'
    hosts = ('kick.com', 'www.kick.com')
    path_pattern = '/[A-Za-z0-9_-]+/?'
    content_selector = '#channel-content'
    ready_selector = '#channel-content h1'
    catalog_url = 'https://kick.com/'
    catalog_selector = 'main a[href]'
    ready_text_pattern = r"фолловер|followers|информация о пользователе|about"
