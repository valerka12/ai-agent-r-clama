"""Twitch: чтение видимой страницы через временный браузер, без API."""

from wb_parser import VisibleBrowserSource


class TwitchWebSource(VisibleBrowserSource):
    platform = 'Twitch'
    table = 'streamers'
    hosts = ('www.twitch.tv', 'twitch.tv')
    path_pattern = '/[A-Za-z0-9_]+/?'
    content_selector = '[data-a-target="about-panel"]'
    ready_selector = '[data-a-target="about-panel"]'
    catalog_url = 'https://www.twitch.tv/directory/all'
    catalog_selector = 'main a[data-a-target="preview-card-channel-link"]'
