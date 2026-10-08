"""A small Netlify API client for the temporary preview sites: create a site, upload a zip, attach a domain,
write the DNS record, and tear it all down. Standard library only; `request` can be replaced in tests."""
import json
import urllib.error
import urllib.request

API = "https://api.netlify.com/api/v1"


class NetlifyError(Exception):
    pass


def _http(method, url, token, body=None, ctype="application/json"):
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": ctype, "User-Agent": "mindlab-leadagent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise NetlifyError(f"Netlify {method} {url.replace(API, '')}: {e.code} {e.read()[:200].decode('utf-8', 'replace')}")
    except (urllib.error.URLError, OSError) as e:
        raise NetlifyError(f"Netlify unreachable: {e}")
    return json.loads(raw) if raw else {}


class Client:
    def __init__(self, token, account_slug="", request=None):
        if not token:
            raise NetlifyError("NETLIFY_AUTH_TOKEN is not set")
        self.token, self.account, self._req = token, account_slug, request or _http

    def _call(self, method, path, payload=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        return self._req(method, API + path, self.token, body, ctype)

    def create_site(self, name):
        path = f"/{self.account}/sites" if self.account else "/sites"
        return self._call("POST", path, {"name": name})

    def deploy_zip(self, site_id, zip_bytes):
        return self._call("POST", f"/sites/{site_id}/deploys", raw=zip_bytes, ctype="application/zip")

    def set_custom_domain(self, site_id, domain):
        return self._call("PATCH", f"/sites/{site_id}", {"custom_domain": domain})

    def provision_ssl(self, site_id):
        return self._call("POST", f"/sites/{site_id}/ssl")

    def dns_zone(self, domain):
        path = "/dns_zones" + (f"?account_slug={self.account}" if self.account else "")
        for z in self._call("GET", path) or []:
            if z.get("name") == domain:
                return z
        return None

    def dns_records(self, zone_id):
        return self._call("GET", f"/dns_zones/{zone_id}/dns_records") or []

    def create_cname(self, zone_id, hostname, value):
        return self._call("POST", f"/dns_zones/{zone_id}/dns_records", {"type": "CNAME", "hostname": hostname, "value": value, "ttl": 3600})

    def delete_dns_record(self, zone_id, record_id):
        return self._call("DELETE", f"/dns_zones/{zone_id}/dns_records/{record_id}")

    def delete_site(self, site_id):
        return self._call("DELETE", f"/sites/{site_id}")
