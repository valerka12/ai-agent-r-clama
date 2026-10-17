"""YouTube: чтение видимой страницы через временный браузер, без API."""

from wb_parser import VisibleBrowserSource


class YouTubeWebSource(VisibleBrowserSource):
    platform = 'YouTube'
    table = 'advertisers'
    hosts = ('www.youtube.com', 'youtube.com')
    path_pattern = '/(?:@[A-Za-z0-9_.-]+|channel/[A-Za-z0-9_-]+)(?:/about)?/?'
    # В новой версии страницы канала заголовок находится вне main.
    # Не читаем рекомендации и чужие видео как сведения о бренде.
    content_selector = 'yt-page-header-renderer'
    ready_selector = 'yt-page-header-renderer h1'
