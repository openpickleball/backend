"""allauth's OpenAPI page loads only same-origin files (issue #102).

The Content Security Policy allows scripts and styles from this origin
only, with no inline script. allauth's default page loads its script from
a CDN, so OPEN's template serves django-ninja's bundled Swagger UI from
the static files instead.
"""

import json
from html.parser import HTMLParser

import pytest
from django.conf import settings
from django.templatetags.static import static
from django.test import Client
from django.urls import reverse

PAGE = "/_allauth/openapi.html"
SETTINGS_TYPE = "application/json"


class _Assets(HTMLParser):
    """Collects the scripts and links of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.links: list[dict[str, str | None]] = []
        self.inline: list[tuple[str | None, str]] = []
        self._in_script: str | None = None
        self._script_type: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self.scripts.append(values)
            self._in_script = values.get("id") or ""
            self._script_type = values.get("type")
        elif tag == "link":
            self.links.append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = None

    def handle_data(self, data: str) -> None:
        if self._in_script is not None and data.strip():
            self.inline.append((self._script_type, data))


@pytest.fixture
def page() -> _Assets:
    response = Client().get(PAGE)
    assert response.status_code == 200
    parser = _Assets()
    parser.feed(response.content.decode())
    return parser


def test_the_page_uses_opens_template() -> None:
    assert settings.HEADLESS_SPECIFICATION_TEMPLATE_NAME == (
        "headless/spec/swagger.html"
    )


def test_every_script_is_a_same_origin_static_file(page: _Assets) -> None:
    sources = [script.get("src") for script in page.scripts if script.get("src")]
    assert sources == [
        static("ninja/swagger-ui-bundle.js"),
        static("ninja/swagger-ui-init.js"),
    ]


def test_every_stylesheet_is_a_same_origin_static_file(page: _Assets) -> None:
    hrefs = [link.get("href") for link in page.links]
    assert hrefs == [static("ninja/swagger-ui.css")]


def test_no_inline_script_runs(page: _Assets) -> None:
    # The only inline block is JSON data, which browsers never run.
    assert [kind for kind, _ in page.inline] == [SETTINGS_TYPE]


def test_the_settings_point_at_allauths_document(page: _Assets) -> None:
    _, data = page.inline[0]
    assert json.loads(data) == {"url": reverse("headless:openapi_json")}
