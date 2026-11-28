import http.server
import socket
import threading
import unittest
from unittest import mock

from support import main


def desc(maker, model, name="TV", extra=""):
    return ('<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:av="urn:schemas-sony-com:av"><device>'
            f"<friendlyName>{name}</friendlyName><manufacturer>{maker}</manufacturer>"
            f"<modelName>{model}</modelName>{extra}</device></root>")


BRAVIA_DMR = desc("Sony Corporation", "KD-65A85", "BRAVIA", "<av:X_ScalarWebAPI_DeviceInfo/>")
BRAVIA_CAST = desc("Sony", "BRAVIA 4K UR3", "Living room TV")  # what some Google TV models answer with
ROUTER = desc("Netcraze Ltd.", "Gateway")
PLAYSTATION = desc("Sony Interactive Entertainment", "PlayStation 5")
HANDYCAM = desc("Sony", "Handycam")


class Parser(unittest.TestCase):
    def test_bravia_by_scalar_web_api(self):
        self.assertEqual(main._parse_tv_description(BRAVIA_DMR), ("BRAVIA", "KD-65A85"))

    def test_bravia_by_name_in_a_chromecast_description(self):
        self.assertEqual(main._parse_tv_description(BRAVIA_CAST), ("Living room TV", "BRAVIA 4K UR3"))

    def test_other_devices_are_ignored(self):
        for xml in (ROUTER, PLAYSTATION, HANDYCAM, "<not xml", ""):
            self.assertIsNone(main._parse_tv_description(xml), xml[:40])

    def test_entities_and_look_alike_tags(self):
        xml = ("<root><device><friendlyName>BRAVIA &amp; Co</friendlyName><manufacturer>Sony</manufacturer>"
               "<manufacturerURL>http://sony.net/</manufacturerURL><modelName>KD-1</modelName>"
               "<modelNumber>XR</modelNumber></device></root>")
        self.assertEqual(main._parse_tv_description(xml), ("BRAVIA & Co", "KD-1"))


class Server:
    """A tiny HTTP server on 127.0.0.1 serving device descriptions."""

    def __init__(self, pages):
        pages_ = pages

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages_.get(self.path, "").encode()
                self.send_response(200 if body else 404)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Discover(unittest.TestCase):
    def setUp(self):
        self.srv = Server({"/dmr.xml": BRAVIA_DMR, "/cast.xml": BRAVIA_CAST, "/router.xml": ROUTER,
                           "/ps.xml": PLAYSTATION, "/handycam.xml": HANDYCAM})
        self.addCleanup(self.srv.close)
        p = mock.patch.object(main, "_log", lambda m: None)
        p.start()
        self.addCleanup(p.stop)

    def found(self, paths):
        with mock.patch.object(main, "_ssdp_search", lambda timeout: {"127.0.0.1": [self.srv.url(p) for p in paths]}):
            return main._discover()

    def test_chromecast_style_tv_is_found(self):
        self.assertEqual(self.found(["/cast.xml"]), [{"ip": "127.0.0.1", "name": "Living room TV", "model": "BRAVIA 4K UR3", "kind": "sony"}])

    def test_second_description_of_the_same_device_is_used(self):
        tv = self.found(["/handycam.xml", "/dmr.xml"])
        self.assertEqual([t["model"] for t in tv], ["KD-65A85"])

    def test_not_a_tv(self):
        for path in ("/router.xml", "/ps.xml", "/handycam.xml", "/missing.xml"):
            self.assertEqual(self.found([path]), [], path)

    def test_every_ssdp_responder_is_checked_beyond_the_old_limit(self):
        locations = {f"192.168.1.{i}": ["description"] for i in range(1, 25)}
        with mock.patch.object(main, "_ssdp_search", return_value=locations), \
                mock.patch.object(main, "_fetch_description", return_value=BRAVIA_DMR), \
                mock.patch.object(main, "_log", lambda _message: None):
            found = main._discover()
        self.assertEqual(len(found), 24)
        self.assertEqual({tv["ip"] for tv in found}, set(locations))

    def test_a_description_on_a_foreign_address_is_never_fetched(self):
        self.assertIsNone(main._fetch_description("10.0.0.5", self.srv.url("/dmr.xml")))


class DescriptionRedirect(unittest.TestCase):
    def test_only_http_redirects_to_the_responder_are_allowed(self):
        handler = main._DescriptionRedirect()
        request = main.urllib.request.Request("http://192.0.2.10/device.xml")
        for target in ("http://192.0.2.20/device.xml", "https://192.0.2.10/device.xml"):
            self.assertIsNone(handler.redirect_request(request, None, 302, "", {}, target))
        target = "http://192.0.2.10/description.xml"
        self.assertEqual(handler.redirect_request(request, None, 302, "", {}, target).full_url, target)


class SsdpSocket(unittest.TestCase):
    """The real UDP code against a responder on the loopback interface."""

    def test_answers_are_collected_once_per_distinct_description(self):
        responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        responder.bind(("127.0.0.1", 0))
        responder.settimeout(3)

        def answer():
            try:
                while True:
                    data, addr = responder.recvfrom(2048)
                    st = [l for l in data.decode().split("\r\n") if l.upper().startswith("ST:")][0][3:].strip()
                    for loc in ("/ssdp/device-desc.xml", "/dmr.xml"):
                        responder.sendto(f"HTTP/1.1 200 OK\r\nST: {st}\r\nLOCATION: http://127.0.0.1:1{loc}\r\n\r\n".encode(), addr)
            except OSError:
                pass

        threading.Thread(target=answer, daemon=True).start()
        self.addCleanup(responder.close)
        with mock.patch.object(main, "SSDP_ADDR", responder.getsockname()), mock.patch.object(main, "_log", lambda m: None):
            found = main._ssdp_search(0.8)
        self.assertEqual(found, {"127.0.0.1": ["http://127.0.0.1:1/ssdp/device-desc.xml", "http://127.0.0.1:1/dmr.xml"]})

    def test_nobody_answers(self):
        with mock.patch.object(main, "SSDP_ADDR", ("127.0.0.1", 9)), mock.patch.object(main, "_log", lambda m: None):
            self.assertEqual(main._ssdp_search(0.3), {})


if __name__ == "__main__":
    unittest.main()
