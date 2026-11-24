import http.server
import threading
import time
import unittest
import unittest.mock

from trackfixer.connection_checks import check_dawarich, check_email, check_trainlog
from trackfixer.dawarich import DawarichError
from trackfixer.trainlog import TrainlogError, check_login, download_trips_csv
from trackfixer.web import create_app

from .helpers import APP_TRACKER_ID, FakeDawarich, temporary_settings

EXPORT_CSV = "uid,path,utc_start_datetime,utc_end_datetime\n1,abc,2026-11-10 11:16:00,2026-11-10 11:25:00\n"


class FakeTrainlog(http.server.BaseHTTPRequestHandler):
    """trainlog's login (?raw=1) and export, for the user "ada" with the password "secret"."""

    def do_POST(self):
        form = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if self.path == "/login/?raw=1" and form == "username=ada&password=secret":
            self.answer(200, "text/plain", "ok", {"Set-Cookie": "session=1; Path=/"})
        else:
            self.answer(401, "text/plain", "no")

    def do_GET(self):
        if self.path == "/u/ada/export" and self.headers.get("Cookie") == "session=1":
            self.answer(200, "text/csv", EXPORT_CSV)
        elif self.path.endswith("/export"):
            self.answer(200, "text/html", "<html>log in first</html>")  # what trainlog does without a session
        else:
            self.answer(404, "text/plain", "not found")

    def answer(self, status, content_type, text, headers=None):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):
        pass


class TrainlogLoginTest(unittest.TestCase):
    def setUp(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTrainlog)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"

    def test_a_working_login(self):
        check_login(self.url, "ada", "secret")
        self.assertEqual(download_trips_csv(self.url, "ada", "secret"), EXPORT_CSV)

    def test_a_wrong_password(self):
        with self.assertRaisesRegex(TrainlogError, "rejected the username or password"):
            check_login(self.url, "ada", "wrong")

    def test_nothing_listening(self):
        with self.assertRaisesRegex(TrainlogError, "Could not reach trainlog"):
            check_login("http://127.0.0.1:9", "ada", "secret")

    def test_the_check_says_what_happened(self):
        settings = temporary_settings(trainlog_url=self.url, trainlog_username="ada", trainlog_password="secret")
        self.assertEqual(check_trainlog(settings), (True, "Logged in as ada; the trip export is available."))

        ok, message = check_trainlog(temporary_settings(trainlog_url=self.url, trainlog_username="ada",
                                                        trainlog_password="wrong"))
        self.assertFalse(ok)
        self.assertIn("rejected the username or password", message)

        ok, message = check_trainlog(temporary_settings())
        self.assertFalse(ok)
        self.assertIn("No trainlog login is set", message)


class DawarichCheckTest(unittest.TestCase):
    def check_with(self, dawarich):
        with unittest.mock.patch("trackfixer.connection_checks.DawarichClient", return_value=dawarich):
            return check_dawarich(temporary_settings())

    def test_counts_the_phone_points_of_the_last_day(self):
        now = int(time.time())
        dawarich = FakeDawarich([
            {"id": 1, "timestamp": now - 3600, "latitude": "50", "longitude": "10", "tracker_id": APP_TRACKER_ID},
            {"id": 2, "timestamp": now - 60, "latitude": "50", "longitude": "10", "tracker_id": "trainlog-1"},
            {"id": 3, "timestamp": now - 3 * 24 * 3600, "latitude": "50", "longitude": "10", "tracker_id": ""},
        ])
        ok, message = self.check_with(dawarich)
        self.assertTrue(ok)
        self.assertIn("Connected. 1 phone points in the last 24 hours", message)

    def test_a_wrong_api_key(self):
        dawarich = unittest.mock.Mock()
        dawarich.get_points.side_effect = DawarichError("HTTP 401", status_code=401)
        self.assertEqual(self.check_with(dawarich), (False, "Dawarich rejected the API key (DAWARICH_API_KEY)."))

    def test_no_dawarich_there(self):
        dawarich = unittest.mock.Mock()
        dawarich.get_points.side_effect = DawarichError("Could not reach Dawarich at http://dawarich.test: refused")
        self.assertEqual(self.check_with(dawarich),
                         (False, "Could not reach Dawarich at http://dawarich.test: refused"))


class EmailCheckTest(unittest.TestCase):
    def test_sends_the_test_email(self):
        settings = temporary_settings(smtp_host="mail.test", email_to="me@example.test", email_from="app@example.test")
        with unittest.mock.patch("smtplib.SMTP") as smtp:
            ok, message = check_email(settings)

        self.assertTrue(ok)
        self.assertIn("Sent to me@example.test", message)
        sent = smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(sent["Subject"], "Dawarich track fixer: test email")

    def test_says_why_sending_failed(self):
        settings = temporary_settings(smtp_host="mail.test", email_to="me@example.test")
        with unittest.mock.patch("smtplib.SMTP", side_effect=ConnectionRefusedError(61, "Connection refused")):
            ok, message = check_email(settings)
        self.assertFalse(ok)
        self.assertIn("Sending failed: [Errno 61] Connection refused", message)

    def test_not_configured(self):
        self.assertEqual(check_email(temporary_settings()),
                         (False, "No mail server or recipient is set (SMTP_HOST and EMAIL_TO)."))


class ConnectionPageTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(temporary_settings()).test_client()

    def test_the_overview_has_a_test_button_for_each_connection(self):
        page = self.client.get("/").get_data(as_text=True)
        for name in ("dawarich", "trainlog", "email"):
            self.assertIn(f'data-test-url="/connections/{name}/test"', page)

    def test_a_test_answers_with_its_result(self):
        response = self.client.post("/connections/email/test")
        self.assertEqual(response.get_json(),
                         {"ok": False, "message": "No mail server or recipient is set (SMTP_HOST and EMAIL_TO)."})
        self.assertEqual(self.client.post("/connections/something/test").status_code, 404)


if __name__ == "__main__":
    unittest.main()
