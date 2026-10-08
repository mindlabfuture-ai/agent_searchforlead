import io, unittest, urllib.error
from unittest import mock

from leadagent import search


def http_error(code, body=b'{"error": "bad query"}'):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


class SearchRetryTests(unittest.TestCase):
    def test_http_error_becomes_a_search_error_with_the_reason(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400)):
            with self.assertRaises(search.SearchError) as cm:
                search._get_json("https://x", {})
        self.assertEqual(cm.exception.status, 400); self.assertIn("bad query", str(cm.exception))

    def test_quoted_query_is_retried_without_quotes_on_400(self):
        seen = []
        def fn(q):
            seen.append(q)
            if '"' in q: raise search.SearchError(400, "nope")
            return [{"url": "https://a.ph"}]
        self.assertEqual(search.search_once(fn, '"proof of payment" GCash "order number"'), [{"url": "https://a.ph"}])
        self.assertEqual(seen[1], "proof of payment GCash order number")

    def test_other_failures_are_not_retried(self):
        calls = []
        def fn(q): calls.append(q); raise search.SearchError(429, "slow down")
        with self.assertRaises(search.SearchError): search.search_once(fn, '"a" b')
        self.assertEqual(len(calls), 1)
        calls.clear()
        def plain(q): calls.append(q); raise search.SearchError(400, "bad")
        with self.assertRaises(search.SearchError): search.search_once(plain, "no quotes here")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
