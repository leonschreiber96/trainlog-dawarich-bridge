import unittest
import unittest.mock

from trackfixer.dawarich import POINTS_PER_DELETE_REQUEST, DawarichClient, DawarichError


class DawarichClientThatFailsAtFirst(DawarichClient):
    """Answers the first `failures` requests with the given HTTP status, then succeeds."""

    def __init__(self, failures, status_code):
        super().__init__("http://dawarich.test", "key")
        self.failures_left = failures
        self.status_code = status_code
        self.requests = []

    def _call(self, method, path, query=None, body=None):
        self.requests.append(body)
        if self.failures_left:
            self.failures_left -= 1
            raise DawarichError("failed", status_code=self.status_code)
        return {}, {}


@unittest.mock.patch("trackfixer.dawarich.time.sleep")
class DeletePointsTest(unittest.TestCase):
    def test_deletes_in_small_batches(self, sleep):
        dawarich = DawarichClientThatFailsAtFirst(failures=0, status_code=None)
        dawarich.delete_points(list(range(POINTS_PER_DELETE_REQUEST * 2 + 1)))
        self.assertEqual([len(body["point_ids"]) for body in dawarich.requests],
                         [POINTS_PER_DELETE_REQUEST, POINTS_PER_DELETE_REQUEST, 1])

    def test_retries_a_batch_after_a_server_error(self, sleep):
        dawarich = DawarichClientThatFailsAtFirst(failures=2, status_code=500)
        dawarich.delete_points([1, 2, 3])
        self.assertEqual(len(dawarich.requests), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_the_last_retry(self, sleep):
        dawarich = DawarichClientThatFailsAtFirst(failures=10, status_code=500)
        with self.assertRaises(DawarichError):
            dawarich.delete_points([1, 2, 3])
        self.assertEqual(len(dawarich.requests), 4)

    def test_does_not_retry_when_the_request_itself_is_wrong(self, sleep):
        dawarich = DawarichClientThatFailsAtFirst(failures=1, status_code=401)
        with self.assertRaises(DawarichError):
            dawarich.delete_points([1, 2, 3])
        self.assertEqual(len(dawarich.requests), 1)


if __name__ == "__main__":
    unittest.main()
