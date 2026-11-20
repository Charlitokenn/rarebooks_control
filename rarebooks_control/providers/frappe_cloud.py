"""The Frappe Cloud API, the only place the control site talks to it (spec 0010 AC-13, spec 0011).

Three methods only: `press.api.site.get`, `press.api.site.update_config` and `press.api.site.archive`. The key,
secret and team come from the control site's `site_config.json` (`frappe_cloud_api_key`,
`frappe_cloud_api_secret`, `frappe_cloud_team`). The key can act on every site of the team, so it is treated
like a root password: no key, secret, config value or URL is ever logged or put in an error text. Tests patch
`get_site`, `update_config` and `archive`.
"""

import json

import frappe
import requests
from urllib3.exceptions import NameResolutionError, NewConnectionError

# frappecloud.com now answers every API call with a 308 to cloud.frappe.io. The client never follows redirects
# (an archive must not be sent twice, spec 0011 AC-1), so it calls the new host directly (spec 0010 AC-13).
BASE_URL = "https://cloud.frappe.io"
TIMEOUT_SECONDS = 30
CONF_KEYS = ("frappe_cloud_api_key", "frappe_cloud_api_secret", "frappe_cloud_team")
# Frappe Cloud's site status once an archive has finished.
ARCHIVED = "Archived"
ACTIVE = "Active"


class FrappeCloudError(Exception):
	"""Frappe Cloud answered with an error, or couldn't be reached before the request was sent. The text names
	only the method and the HTTP status, never a credential or a config value."""


class OutcomeUnknown(FrappeCloudError):
	"""The request was sent, but no answer came (a timeout or a reset). For `archive` this counts as requested
	(spec 0011 AC-1): the irreversible side may already have happened."""


def get_site(name: str) -> dict:
	"""The site's `name`, `status` and `group`. `group` is empty unless the team owns the bench group, and an
	empty one fails every bench group check (spec 0011 AC-3)."""
	site = _call("get", {"name": name})
	if not isinstance(site, dict):
		raise FrappeCloudError("press.api.site.get answered without a site")
	return {
		"name": site.get("name") or None,
		"status": site.get("status") or None,
		"group": site.get("group") or None,
	}


def update_config(name: str, config: dict):
	"""Write string keys into the site's `site_config.json`. Frappe Cloud returns the site's config rows; nothing
	from them is kept, since only a verified `control_ping` proves the write took effect (spec 0010 AC-11)."""
	rows = [{"key": key, "value": value, "type": "String"} for key, value in config.items()]
	_call("update_config", {"name": name, "config": json.dumps(rows)})


def archive(name: str):
	"""Ask Frappe Cloud to archive the site, with `force` off. Returns nothing. Raises `OutcomeUnknown` when the
	request was sent and no answer came, and `FrappeCloudError` on a clear failure (spec 0011 AC-1)."""
	_call("archive", {"name": name, "force": False})


def _call(method: str, payload: dict):
	path = f"press.api.site.{method}"
	key, secret, team = _credentials()
	try:
		response = requests.post(
			f"{BASE_URL}/api/method/{path}",
			json=payload,
			headers={"Authorization": f"token {key}:{secret}", "X-Press-Team": team},
			timeout=TIMEOUT_SECONDS,
			allow_redirects=False,
		)
	except requests.RequestException as error:
		if _not_sent(error):
			raise FrappeCloudError(f"{path} could not be reached") from None
		raise OutcomeUnknown(f"{path} sent, no answer ({type(error).__name__})") from None
	if response.status_code != 200:
		raise FrappeCloudError(f"{path} answered HTTP {response.status_code}")
	try:
		data = response.json()
	except ValueError:
		raise FrappeCloudError(f"{path} answered with invalid JSON") from None
	return data.get("message") if isinstance(data, dict) else None


def _not_sent(error: requests.RequestException) -> bool:
	"""True when the request surely never reached Frappe Cloud: a connect timeout, a failed DNS lookup or TCP
	connect, or a TLS handshake that failed. Anything else (a read timeout, a reset while waiting) may have been
	received, so it is an unknown outcome."""
	if isinstance(error, (requests.ConnectTimeout, requests.exceptions.SSLError)):
		return True
	if isinstance(error, requests.ConnectionError):
		cause = error.args[0] if error.args else None
		reason = getattr(cause, "reason", cause)
		return isinstance(reason, (NewConnectionError, NameResolutionError))
	return isinstance(error, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema))


def _credentials() -> tuple[str, str, str]:
	values = [frappe.conf.get(key) for key in CONF_KEYS]
	if not all(isinstance(value, str) and value for value in values):
		raise FrappeCloudError("Frappe Cloud API access is not set in site_config.json")
	return tuple(values)
