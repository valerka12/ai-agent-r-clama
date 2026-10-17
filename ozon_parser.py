"""Ozon: чтение видимой страницы через временный браузер, без API."""

from wb_parser import VisibleBrowserSource


class OzonWebSource(VisibleBrowserSource):
    platform = 'Ozon'
    table = 'advertisers'
    hosts = ('www.ozon.ru', 'ozon.ru')
    path_pattern = '/product/[a-zA-Z0-9_-]+/?'
    content_selector = '[data-widget="webProductHeading"]'
    ready_selector = '[data-widget="webProductHeading"] h1'

    def normalize_url(self, value):
        from urllib.parse import urlsplit, urlunsplit, parse_qsl
        from wb_parser import SourceError
        from validation import validate_url
        validate_url(value)
        parsed = urlsplit(value)
        # Ozon добавляет эти параметры к той же карточке автоматически.
        # Параметры выбора товара и произвольные параметры не отбрасываем.
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if not pairs or any(key not in {'sh', 'at', '__rr', 'abt_att'} for key, _ in pairs):
                raise SourceError('Ozon: неизвестные параметры ссылки; нужна прямая карточка.')
            value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', parsed.fragment))
        return super().normalize_url(value)

    def _capture(self, page):
        capture = super()._capture(page)
        scores = page.locator('[data-widget="webSingleProductScore"]').all_text_contents()
        capture['text'] += '\n' + '\n'.join(scores)
        capture['product_metrics'] = self._product_metrics(scores)
        return capture

    @staticmethod
    def _product_metrics(scores):
        import re
        values = set()
        for text in scores:
            match = re.fullmatch(r'\s*([0-5][.,][0-9])\s*[•·]\s*([0-9][0-9\s]*)\s+отзыв\w*\s*', text)
            if match:
                values.add((float(match[1].replace(',', '.')), int(re.sub(r'\s', '', match[2]))))
        if len(values) != 1:
            return {}
        rating, count = values.pop()
        if rating > 5:
            return {}
        return {'reviewRating': rating, 'feedbacks': count}

    catalog_url = 'https://www.ozon.ru/'
    catalog_selector = '[data-widget="tileGridDesktop"] a[href*="/product/"]'
