import io
import os
from dataclasses import replace
import unittest
from unittest.mock import patch

# The module WSGI entry point deliberately requires an explicit production
# authentication choice. Unit tests exercise unauthenticated behavior through
# this test-only opt-out.
os.environ.setdefault("HTML2PDF__ALLOW_UNAUTHENTICATED", "true")

import html2pdf.app as app
from html2pdf.app import AssetError, AssetPolicy, render_document
from weasyprint.urls import URLFetcherResponse


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = app.Config(
            token="", version="test", workers=2, timeout=45,
            max_html_bytes=1024, max_pdf_bytes=65536,
        )
        self.application = app.create_application(self.config)

    def configure(self, **overrides):
        self.config = replace(self.config, **overrides)
        self.application = app.create_application(self.config)

    def request(self, method="POST", path="/render", body=b"<p>Hello</p>", application=None, **overrides):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_TYPE": "text/html; charset=utf-8",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        environ.update(overrides)
        response = {}

        def start(status, headers):
            response["status"] = status
            response["headers"] = dict(headers)

        response["body"] = b"".join(
            (application or self.application)(environ, start))
        return response

    def test_index(self):
        result = self.request(method="GET", path="/")
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(
            result["headers"]["Content-Type"],
            "text/html; charset=utf-8",
        )
        self.assertIn(b"html2pdf", result["body"])
        self.assertEqual(result["body"].count(
            b'href="https://github.com/gi8lino/html2pdf"'), 1)
        self.assertIn(b'class="copy-button"', result["body"])
        self.assertIn(b'href="/logo.svg"', result["body"])
        self.assertIn(
            f'<span class="version">{self.config.version}</span>'.encode(),
            result["body"],
        )
        self.assertIn(b"HTML2PDF__ASSET_POLICY", result["body"])
        self.assertIn(b"<code>embedded</code>", result["body"])
        csp = result["headers"]["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(result["headers"]["X-Frame-Options"], "DENY")
        self.assertEqual(result["headers"]["Referrer-Policy"], "no-referrer")

    def test_applications_keep_configuration_and_pages_isolated(self):
        original = self.application
        self.configure(token="secret", version="other", max_html_bytes=1)
        with patch("html2pdf.app.render_document", return_value=b"%PDF-fixture"):
            self.assertEqual(self.request()["status"], "401 Unauthorized")
            self.assertEqual(
                self.request(HTTP_AUTHORIZATION="Bearer secret")["status"],
                "413 Content Too Large",
            )
            self.assertEqual(self.request(application=original)[
                             "status"], "200 OK")
        for application, version in ((original, "test"), (self.application, "other")):
            result = self.request(method="GET", path="/",
                                  application=application)
            self.assertIn(
                f'<span class="version">{version}</span>'.encode(), result["body"])

    def test_assets_are_loaded_once_per_application(self):
        with (
            patch("html2pdf.app.load_index", return_value=b"cached page") as index,
            patch.object(app.Path, "read_bytes", return_value=b"cached logo") as logo,
        ):
            application = app.create_application(self.config)
            for _ in range(2):
                self.assertEqual(
                    self.request(method="GET", path="/",
                                 application=application)["body"],
                    b"cached page",
                )
                self.assertEqual(
                    self.request(method="GET", path="/logo.svg",
                                 application=application)["body"],
                    b"cached logo",
                )
            index.assert_called_once_with(self.config)
            logo.assert_called_once_with()

    def test_module_wsgi_entry_point_serves_health(self):
        result = self.request(method="GET", path="/healthz",
                              application=app.application)
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(result["body"], b"ok\n")

    def test_logo(self):
        result = self.request(method="GET", path="/logo.svg")
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(result["headers"]["Content-Type"], "image/svg+xml")
        self.assertTrue(result["body"].startswith(b"<svg"))

    def test_health(self):
        result = self.request(method="GET", path="/healthz")
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(result["body"], b"ok\n")

    def test_http_contract(self):
        with patch("html2pdf.app.render_document", return_value=b"%PDF-fixture") as render:
            result = self.request()
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(result["headers"]["Content-Type"], "application/pdf")
        self.assertEqual(result["body"], b"%PDF-fixture")
        render.assert_called_once_with("<p>Hello</p>", AssetPolicy.EMBEDDED)

    def test_render_logging(self):
        with self.assertLogs("gunicorn.error", level="INFO") as logs:
            with patch("html2pdf.app.render_document", return_value=b"%PDF-fixture"):
                result = self.request()

        self.assertEqual(result["status"], "200 OK")
        self.assertIn("render completed", logs.output[-1])
        self.assertIn("html_bytes=12", logs.output[-1])
        self.assertIn("pdf_bytes=12", logs.output[-1])
        self.assertIn("duration_ms=", logs.output[-1])

    def test_rejected_render_logging(self):
        with self.assertLogs("gunicorn.error", level="WARNING") as logs:
            with patch("html2pdf.app.render_document", side_effect=AssetError):
                result = self.request()

        self.assertEqual(result["status"], "422 Unprocessable Content")
        self.assertIn("render rejected", logs.output[-1])
        self.assertIn("reason=asset_policy", logs.output[-1])
        self.assertIn("html_bytes=12", logs.output[-1])

    def test_optional_bearer_authentication(self):
        self.configure(token="secret")
        missing = self.request()
        wrong = self.request(HTTP_AUTHORIZATION="Bearer nope")
        with patch("html2pdf.app.render_document", return_value=b"%PDF-fixture"):
            valid = self.request(HTTP_AUTHORIZATION="Bearer secret")

        self.assertEqual(missing["status"], "401 Unauthorized")
        self.assertEqual(missing["headers"]["WWW-Authenticate"], "Bearer")
        self.assertEqual(wrong["status"], "401 Unauthorized")
        self.assertEqual(valid["status"], "200 OK")

    def test_authentication_does_not_protect_index_or_health(self):
        self.configure(token="secret")
        self.assertEqual(self.request(method="GET", path="/")
                         ["status"], "200 OK")
        self.assertEqual(
            self.request(method="GET", path="/healthz")["status"],
            "200 OK",
        )
        self.assertEqual(
            self.request(method="GET", path="/logo.svg")["status"],
            "200 OK",
        )

    def test_routes_and_validation(self):
        cases = [
            ({"method": "GET"}, 405),
            ({"path": "/render/"}, 404),
            ({"CONTENT_TYPE": "application/json"}, 415),
            ({"CONTENT_LENGTH": ""}, 411),
            ({"CONTENT_LENGTH": "bad"}, 400),
            ({"CONTENT_LENGTH": "-1"}, 400),
            ({"CONTENT_LENGTH": str(self.config.max_html_bytes + 1)}, 413),
            ({"CONTENT_LENGTH": "1000"}, 400),
            ({"body": b"\xff"}, 400),
        ]

        for args, expected in cases:
            with self.subTest(args=args), patch("html2pdf.app.render_document") as render:
                result = self.request(**args)
                self.assertEqual(int(result["status"].split()[0]), expected)
                render.assert_not_called()

    def test_network_and_file_assets_are_rejected(self):
        urls = [
            "https://example.com/image.png",
            "http://127.0.0.1:8080/private",
            "http://10.0.0.1/private",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
            "ftp://example.com/file",
        ]

        for url in urls:
            with self.subTest(url=url), self.assertRaises(AssetError):
                render_document(f'<img src="{url}">')

    def test_remote_policy_allows_http_and_https_assets(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'

        for url in ("http://example.com/image.svg", "https://example.com/image.svg"):
            with self.subTest(url=url), patch.object(
                app.URLFetcher,
                "fetch",
                return_value=URLFetcherResponse(
                    url,
                    svg,
                    {"Content-Type": "image/svg+xml"},
                ),
            ) as fetch, patch.object(
                app.socket,
                "getaddrinfo",
                return_value=[(0, 0, 0, "", ("93.184.216.34", 443))],
            ):
                result = render_document(
                    f'<img src="{url}">', AssetPolicy.REMOTE, frozenset(("example.com",)),
                )

            self.assertTrue(result.startswith(b"%PDF-"))
            fetch.assert_called_once()

    def test_remote_policy_still_rejects_non_remote_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/file"):
            with (
                self.subTest(url=url),
                patch.object(app.URLFetcher, "fetch") as fetch,
                self.assertRaises(AssetError),
            ):
                render_document(f'<img src="{url}">', AssetPolicy.REMOTE, frozenset(("example.com",)))

            fetch.assert_not_called()

    def test_data_url_assets_are_allowed(self):
        source = (
            '<img alt="pixel" '
            'src="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%221%22%20height%3D%221%22%2F%3E">'
        )
        result = render_document(source)
        self.assertTrue(result.startswith(b"%PDF-"))

    def test_data_url_svg_cannot_bypass_embedded_policy(self):
        source = (
            '<img src="data:image/svg+xml,'
            '%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2210%22%20height%3D%2210%22%3E'
            '%3Cimage%20href%3D%22http%3A%2F%2F127.0.0.1%3A9%2Fprivate%22%20width%3D%2210%22%20height%3D%2210%22%2F%3E'
            '%3C%2Fsvg%3E">'
        )

        with self.assertRaises(AssetError):
            render_document(source)

    def test_generated_pdf_size_limit(self):
        self.configure(max_pdf_bytes=3)
        with (
            patch("html2pdf.app.render_document", return_value=b"1234"),
            self.assertLogs("gunicorn.error", level="WARNING") as logs,
        ):
            result = self.request()

        self.assertEqual(result["status"], "413 Content Too Large")
        self.assertEqual(
            result["body"], b"PDF exceeds configured size limit\n")
        self.assertIn("reason=pdf_too_large", logs.output[-1])

    def test_malformed_bearer_schemes_are_rejected(self):
        authorizations = [
            "Basic secret",
            "Token secret",
            "Bearer",
            "Bearer   ",
            "Bearer wrong",
        ]

        self.configure(token="secret")
        for authorization in authorizations:
            with self.subTest(authorization=authorization):
                result = self.request(HTTP_AUTHORIZATION=authorization)
                self.assertEqual(result["status"], "401 Unauthorized")

    def test_real_pdf_render(self):
        result = render_document("<h1>Hello</h1><p>Rendered by html2pdf.</p>")
        self.assertTrue(result.startswith(b"%PDF-"))

    def test_non_ascii_authorization_is_rejected_without_rendering(self):
        self.configure(token="secret")
        with (
            patch("html2pdf.app.render_document") as render,
        ):
            result = self.request(HTTP_AUTHORIZATION="Bearer séc ret")
        self.assertEqual(result["status"], "401 Unauthorized")
        render.assert_not_called()

    def test_bearer_scheme_is_case_insensitive(self):
        self.configure(token="secret")
        with (
            patch("html2pdf.app.render_document", return_value=b"%PDF-fixture"),
        ):
            result = self.request(HTTP_AUTHORIZATION="  bEaReR   secret  ")
        self.assertEqual(result["status"], "200 OK")

    def test_authentication_precedes_body_read(self):
        self.configure(token="secret")
        result = self.request(
            CONTENT_TYPE="application/json", **{"wsgi.input": None})
        self.assertEqual(result["status"], "401 Unauthorized")

    def test_content_length_requires_ascii_digits(self):
        for length in ("+12", "1_2", "１２", "١٢", "1.2"):
            with self.subTest(length=length), patch("html2pdf.app.render_document") as render:
                result = self.request(CONTENT_LENGTH=length)
                self.assertEqual(result["status"], "400 Bad Request")
                render.assert_not_called()

    def test_oversized_content_length_is_rejected_before_body_read(self):
        for length in (str(self.config.max_html_bytes + 1), "9" * 5000):
            with self.subTest(digits=len(length)), patch("html2pdf.app.render_document") as render:
                result = self.request(
                    CONTENT_LENGTH=length, **{"wsgi.input": None})
                self.assertEqual(result["status"], "413 Content Too Large")
                render.assert_not_called()

    def test_content_length_with_leading_zeroes(self):
        with patch("html2pdf.app.render_document", return_value=b"%PDF-fixture") as render:
            result = self.request(CONTENT_LENGTH="0" * 5000 + "12")
        self.assertEqual(result["status"], "200 OK")
        render.assert_called_once_with("<p>Hello</p>", AssetPolicy.EMBEDDED)
        result = self.request(CONTENT_LENGTH="0000", **{"wsgi.input": None})
        self.assertEqual(result["status"], "400 Bad Request")

    def test_utf8_charset_declarations(self):
        content_types = (
            "text/html",
            "text/html; charset=utf-8",
            'text/html; CHARSET="UTF-8"',
            "text/html; charset=utf8",
            'text/html; note="a;charset=latin-1"; charset=utf-8',
            'text/html; note="a;charset=latin-1"',
        )
        for content_type in content_types:
            with self.subTest(content_type=content_type), patch(
                "html2pdf.app.render_document", return_value=b"%PDF-fixture"
            ) as render:
                result = self.request(CONTENT_TYPE=content_type)
                self.assertEqual(result["status"], "200 OK")
                render.assert_called_once_with("<p>Hello</p>", AssetPolicy.EMBEDDED)

    def test_unsupported_charsets_are_rejected_before_body_read(self):
        for charset in ("latin-1", "utf-16", '"ISO-8859-1"', ""):
            with self.subTest(charset=charset), patch("html2pdf.app.render_document") as render:
                result = self.request(
                    CONTENT_TYPE=f"text/html; charset={charset}",
                    **{"wsgi.input": None},
                )
                self.assertEqual(result["status"],
                                 "415 Unsupported Media Type")
                render.assert_not_called()

    def test_known_get_endpoints_reject_other_methods(self):
        for path in ("/", "/logo.svg", "/healthz"):
            for method in ("POST", "PUT", "DELETE", "OPTIONS"):
                with self.subTest(path=path, method=method):
                    result = self.request(method=method, path=path)
                    self.assertEqual(result["status"],
                                     "405 Method Not Allowed")
                    self.assertEqual(result["headers"]["Allow"], "GET")
                    self.assertEqual(result["body"], b"Use GET\n")

    def test_empty_body_is_rejected(self):
        with patch("html2pdf.app.render_document") as render:
            result = self.request(body=b"")
        self.assertEqual(result["status"], "400 Bad Request")
        render.assert_not_called()

    def test_html_limit_is_inclusive_and_counts_utf8_bytes(self):
        body = "<p>é</p>".encode()
        self.configure(max_html_bytes=len(body))
        with (
            patch("html2pdf.app.render_document", return_value=b"%PDF-fixture") as render,
        ):
            result = self.request(
                body=body, CONTENT_TYPE="TEXT/HTML; charset=UTF-8")
            self.assertEqual(result["status"], "200 OK")
            render.assert_called_once_with(body.decode(), AssetPolicy.EMBEDDED)
            render.reset_mock()
            result = self.request(body=body + b" ", **{"wsgi.input": None})
            self.assertEqual(result["status"], "413 Content Too Large")
            render.assert_not_called()

    def test_pdf_limit_is_inclusive(self):
        self.configure(max_pdf_bytes=4)
        with (
            patch("html2pdf.app.render_document", return_value=b"1234"),
        ):
            result = self.request()
        self.assertEqual(result["status"], "200 OK")

    def test_internal_render_error_is_logged_but_not_exposed(self):
        with (
            patch("html2pdf.app.render_document",
                  side_effect=RuntimeError("private details")),
            self.assertLogs("gunicorn.error", level="ERROR") as logs,
        ):
            result = self.request()
        self.assertEqual(result["status"], "500 Internal Server Error")
        self.assertEqual(result["body"], b"PDF rendering failed\n")
        self.assertIn("reason=internal_error", logs.output[-1])

    def test_response_headers_and_allowed_method(self):
        for method, path in (("GET", "/"), ("GET", "/logo.svg"),
                             ("GET", "/healthz"), ("GET", "/render"),
                             ("GET", "/missing")):
            with self.subTest(path=path):
                result = self.request(method=method, path=path)
                headers = result["headers"]
                self.assertEqual(
                    int(headers["Content-Length"]), len(result["body"]))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                if path == "/render":
                    self.assertEqual(headers["Allow"], "POST")

    def test_external_stylesheets_and_css_images_never_reach_fetcher(self):
        sources = [
            '<img src="image.png">',
            '<img src="/image.png">',
            '<img src="//example.com/image.png">',
            '<link rel="stylesheet" href="style.css">',
            '<link rel="stylesheet" href="https://example.com/style.css">',
            '<object data="https://example.com/image.svg"></object>',
            '<embed src="https://example.com/image.svg">',
            '<a rel="attachment" href="https://example.com/file">file</a>',
            '<style>@import "file:///etc/passwd";</style>',
            '<style>@font-face { font-family: x; src: url(https://example.com/font.woff2) } body { font-family: x }</style>',
            '<style>body { background-image: url(http://127.0.0.1/private) }</style><p>x</p>',
            '<base href="https://example.com/"><img src="image.png">',
        ]
        for source in sources:
            with (
                self.subTest(source=source),
                patch.object(app.URLFetcher, "fetch") as fetch,
                self.assertRaises(AssetError),
            ):
                render_document(source)
            fetch.assert_not_called()

    def test_inline_css_and_fragment_links_are_allowed(self):
        result = render_document(
            '<style>p { color: red }</style>'
            '<a href="#target">Jump</a><p id="target">Hello</p>'
        )
        self.assertTrue(result.startswith(b"%PDF-"))


class ConfigTests(unittest.TestCase):
    def test_listen_address_is_loaded_and_displayed(self):
        default = app.Config.from_env({"HTML2PDF__ALLOW_UNAUTHENTICATED": "true"})
        self.assertEqual(default.listen_address, "0.0.0.0:8080")
        self.assertIn(b"0.0.0.0:8080", app.load_index(default))
        config = app.Config.from_env({
            "HTML2PDF__LISTEN_ADDRESS": " 0.0.0.0:9090 ",
            "HTML2PDF__ALLOW_UNAUTHENTICATED": "true",
        })
        self.assertEqual(config.listen_address, "0.0.0.0:9090")
        page = app.load_index(config)
        self.assertIn(b"HTML2PDF__LISTEN_ADDRESS", page)
        self.assertIn(b"<code>0.0.0.0:9090</code>", page)
        self.assertNotIn(b"{{LISTEN_ADDRESS}}", page)
        escaped = app.load_index(replace(config, listen_address='<address>"&'))
        self.assertIn(b"&lt;address&gt;&quot;&amp;", escaped)

    def test_defaults_are_independent_of_host_environment(self):
        config = app.Config.from_env({"HTML2PDF__ALLOW_UNAUTHENTICATED": "true"})
        self.assertEqual(config, app.Config("", "dev", 2, 45,
                                            32 * 1024 * 1024, 64 * 1024 * 1024))

    def test_environment_overrides_and_whitespace(self):
        config = app.Config.from_env({
            "HTML2PDF__TOKEN": " secret ", "HTML2PDF__VERSION": " v1 ",
            "HTML2PDF__WORKERS": " 3 ", "HTML2PDF__TIMEOUT": "60",
            "HTML2PDF__MAX_HTML_BYTES": "100", "HTML2PDF__MAX_PDF_BYTES": "200",
            "HTML2PDF__ASSET_POLICY": " REMOTE ",
            "HTML2PDF__REMOTE_ASSET_HOSTS": "assets.example.com",
        })
        self.assertEqual(
            config,
            app.Config("secret", "v1", 3, 60, 100, 200, asset_policy=AssetPolicy.REMOTE,
                       remote_asset_hosts=frozenset(("assets.example.com",))),
        )

    def test_invalid_numeric_configuration_fails_startup(self):
        for name in ("WORKERS", "TIMEOUT", "MAX_HTML_BYTES", "MAX_PDF_BYTES"):
            for value in ("0", "-1", "nope", "1.5"):
                key = f"HTML2PDF__{name}"
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    app.Config.from_env({"HTML2PDF__TOKEN": "secret", key: value})

    def test_blank_values_use_defaults(self):
        config = app.Config.from_env({
            "HTML2PDF__VERSION": " ",
            "HTML2PDF__LISTEN_ADDRESS": "",
            "HTML2PDF__WORKERS": " ",
            "HTML2PDF__ASSET_POLICY": " ",
            "HTML2PDF__ALLOW_UNAUTHENTICATED": "true",
        })
        self.assertEqual(config.version, "dev")
        self.assertEqual(config.listen_address, "0.0.0.0:8080")
        self.assertEqual(config.workers, 2)
        self.assertIs(config.asset_policy, AssetPolicy.EMBEDDED)

    def test_invalid_asset_policy_fails_startup(self):
        for value in ("allow", "all", "http", "file", "nope"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "HTML2PDF__ASSET_POLICY must be one of: embedded, remote",
            ):
                app.Config.from_env({"HTML2PDF__TOKEN": "secret", "HTML2PDF__ASSET_POLICY": value})

    def test_index_escapes_configuration_and_never_displays_token(self):
        config = app.Config('hidden-secret', '<test>',
                            3, 60, 1536, 2048)
        page = app.load_index(config).decode()
        self.assertIn('&lt;test&gt;', page)
        self.assertIn('enabled', page)
        self.assertIn('1.5 KiB', page)
        self.assertIn('2 KiB', page)
        self.assertNotIn('hidden-secret', page)
        self.assertNotIn('{{', page)

    def test_index_preserves_placeholder_text_in_configuration(self):
        config = replace(
            app.Config.from_env({"HTML2PDF__ALLOW_UNAUTHENTICATED": "true"}),
            version="{{WORKERS}}<test>",
            listen_address="{{TIMEOUT}}&address",
        )
        page = app.load_index(config)
        self.assertIn(b'<span class="version">{{WORKERS}}&lt;test&gt;</span>', page)
        self.assertIn(b"<code>{{TIMEOUT}}&amp;address</code>", page)
        self.assertIn(b"<code>45s</code>", page)

    def test_byte_size_formatting(self):
        for value, expected in ((0, "0 B"), (1023, "1023 B"), (1024, "1 KiB"),
                                (1536, "1.5 KiB"), (1024 ** 2, "1 MiB"),
                                (1024 ** 3, "1 GiB")):
            with self.subTest(value=value):
                self.assertEqual(app.format_bytes(value), expected)


if __name__ == "__main__":
    unittest.main()
