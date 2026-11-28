"""The Frappe Cloud API, the only place the control site talks to it (spec 0010 AC-13, spec 0011).

The methods of `press.api.site`: `get`, `update_config` and `archive` (spec 0010), with
`press.api.client.get_list` on Site Config read first so `update_config` keeps the other keys, the seven domain methods
of spec 0012 AC-13 (`domains`, `domain_exists`, `check_dns`, `add_domain`, `retry_add_domain`, `set_host_name`,
`remove_domain`), and `new` for the warm pool (spec 0014 AC-15), with `press.api.bench.get` and
`press.api.server.get` read only to fill `new`'s version and cluster. Names and answers were read from the open
source `press` app and checked live. The key,
secret and team come from the control site's `site_config.json` (`frappe_cloud_api_key`,
`frappe_cloud_api_secret`, `frappe_cloud_team`). The key can act on every site of the team, so it is treated
like a root password: no key, secret, config value or URL is ever logged or put in an error text. Tests patch
every public function.
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
# What `get` answers for an archived site: it is no longer visible to the team (checked live on 2026-11-21). A
# wrong or revoked key answers the same, so a 403 alone never proves an archive (spec 0013 AC-1).
FORBIDDEN = 403
# Frappe Cloud's answer to `check_dns` while the domain doesn't point at the site yet.
DNS_NOT_MATCHED = 417
# The `type` of a Site Config row in press (empty on an old row, which press then types itself).
CONFIG_TYPES = (None, "", "String", "Password", "Number", "Boolean", "JSON")
# The page size for reading Site Config rows: press's own `get_list` default, so no server side cap is smaller.
CONFIG_PAGE = 20
# A stop for a read that never ends: far more rows than a site's config holds.
CONFIG_MAX_ROWS = 2000


class FrappeCloudError(Exception):
	"""Frappe Cloud answered with an error, or couldn't be reached before the request was sent. The text names
	only the method and the HTTP status, never a credential or a config value. `status` is the HTTP status, or
	None when there was no answer."""

	def __init__(self, text: str, status: int | None = None):
		super().__init__(text)
		self.status = status


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
	"""Set string keys in the site's `site_config.json` and keep every other key (spec 0010 AC-11; spec 0011 AC-8).

	`press.api.site.update_config` with a list (the only form the public API takes) goes to
	`Site._set_configuration`, which replaces the site's whole configuration, keeping only blacklisted and
	internal keys; only a dict is merged. Confirmed live on 2026-11-27: a 7 key write was refused because press
	counted every other key as removed. So the site's current rows are read first, `config` is merged over them
	and the full list is sent. A failed or incomplete read raises `FrappeCloudError` and writes nothing, since
	writing only `config` would delete every other key. Frappe Cloud answers with the config rows; nothing from
	them is kept, since only a verified `control_ping` proves the write took effect."""
	rows = _merged_rows(site_config_rows(name), config)
	_call("update_config", {"name": name, "config": json.dumps(rows)})


def site_config_rows(name: str) -> list[dict]:
	"""The site's Site Config rows as `{key, value, type}`, read through `press.api.client.get_list`, which
	needs `parent` and `parenttype` for a child table. A Password row comes back as `*******`. Read in pages of
	press's default size, ordered by `idx`, until a short page, so a server side limit can't cut the list short
	unseen. Raises `FrappeCloudError` when a page can't be read or holds something other than config rows; a
	read changes nothing, so no outcome is unknown (spec 0010 AC-11)."""
	rows = []
	while True:
		try:
			page = _call(
				"get_list",
				{
					"doctype": "Site Config",
					"fields": ["key", "type", "value"],
					"filters": {"parent": name, "parenttype": "Site"},
					"order_by": "idx asc",
					"start": len(rows),
					"limit": CONFIG_PAGE,
				},
				module="client",
			)
		except OutcomeUnknown:
			raise FrappeCloudError("press.api.client.get_list sent, no answer") from None
		if not isinstance(page, list) or not all(_is_config_row(row) for row in page):
			raise FrappeCloudError("press.api.client.get_list answered without Site Config rows")
		rows += [{"key": row["key"], "value": row.get("value"), "type": row.get("type")} for row in page]
		if len(page) < CONFIG_PAGE:
			return rows
		if len(rows) >= CONFIG_MAX_ROWS:
			raise FrappeCloudError("press.api.client.get_list kept answering Site Config rows")


def _is_config_row(row) -> bool:
	return (
		isinstance(row, dict)
		and isinstance(row.get("key"), str)
		and bool(row["key"])
		and row.get("type") in CONFIG_TYPES
	)


def _merged_rows(rows: list[dict], config: dict) -> list[dict]:
	"""`rows` with each key of `config` set as a String: in place of the first row with that key (a repeat of it
	is dropped), else appended. Every other row is sent back as read, with its own type. A Password row goes back
	as the `*******` mask it was read as, and press keeps the stored value when it sees that mask; a Number,
	Boolean or JSON row is parsed again from its stored text (spec 0010 AC-11)."""
	new = {key: {"key": key, "value": value, "type": "String"} for key, value in config.items()}
	merged = []
	for row in rows:
		if row["key"] in config:
			if row["key"] in new:
				merged.append(new.pop(row["key"]))
			continue
		merged.append(row)
	return merged + list(new.values())


def archive(name: str):
	"""Ask Frappe Cloud to archive the site, with `force` off. Returns nothing. Raises `OutcomeUnknown` when the
	request was sent and no answer came, and `FrappeCloudError` on a clear failure (spec 0011 AC-1)."""
	_call("archive", {"name": name, "force": False})


def new_site(label: str, domain: str, group: str, server: str, plan: str, apps: list[str]) -> str:
	"""Ask Frappe Cloud to build `<label>.<domain>` on a bench of `group` on `server`, with `apps` installed
	(spec 0014 AC-15).

	Confirmed live on 2026-11-23 (build task 1): `press.api.site.new` takes one `site` object with `name` (the
	label only; Frappe Cloud joins it to `domain`), `domain`, `group`, `server`, `cluster`, `version`, `plan` and
	`apps` (app names), and answers `{"site": <full name>, "job": <agent job>}`; the team key may create sites,
	and a site was Active on the group in under a minute. Three facts from that run shape the call:
	- Without `server`, a plan with private bench support (the Hetzner plans) skips `group` and makes a whole new
	  bench through a Site Group Deploy. With `server` and `version`, `new` reuses the team's bench of the same
	  version on that server holding every app in `apps`, which is `group`'s own bench.
	- The site's `cluster` must equal the bench's, or Frappe Cloud refuses ("cannot be deployed on None").
	- So `version` is read from the group (`press.api.bench.get`) and `cluster` from the server's region
	  (`press.api.server.get`) just before the call. Both reads send nothing that builds; a failed read raises
	  `FrappeCloudError`, and no site was made.

	Returns the full site name Frappe Cloud answered. Raises `OutcomeUnknown` when the request was sent and no
	answer came (the site may exist), and `FrappeCloudError` on a clear refusal (no site was made)."""
	version, cluster = _bench_version(group), _server_cluster(server)
	site = {
		"name": label,
		"domain": domain,
		"group": group,
		"server": server,
		"cluster": cluster,
		"version": version,
		"plan": plan,
		"apps": apps,
	}
	answer = _call("new", {"site": site})
	name = answer.get("site") if isinstance(answer, dict) else None
	if not isinstance(name, str) or not name:
		# A 200 was answered, so a site may exist: the caller keeps its row, as for an unknown outcome.
		raise OutcomeUnknown("press.api.site.new answered without a site name")
	return name.lower()


def _bench_version(group: str) -> str:
	"""The Frappe version of the team's bench group, for example `Version 16`."""
	bench = _call("get", {"name": group}, module="bench")
	version = bench.get("version") if isinstance(bench, dict) else None
	if not isinstance(version, str) or not version:
		raise FrappeCloudError("press.api.bench.get answered without a version")
	return version


def _server_cluster(server: str) -> str:
	"""The cluster of the team's server, read from its region, for example `Falkenstein-3`."""
	details = _call("get", {"name": server}, module="server")
	region = details.get("region_info") if isinstance(details, dict) else None
	cluster = region.get("name") if isinstance(region, dict) else None
	if not isinstance(cluster, str) or not cluster:
		raise FrappeCloudError("press.api.server.get answered without a region")
	return cluster


# The domain methods (spec 0012 AC-13). Frappe Cloud checks DNS itself, so `add_domain` adds nothing while
# `check_dns` fails; `set_host_name` saves the site at once and refuses a domain that isn't `Active`.


def domains(name: str) -> list[dict]:
	"""The site's domains as `{domain, status, primary}`. `status` is Pending, In Progress, Active or Broken;
	`primary` is true for the site's `host_name`."""
	rows = _call("domains", {"name": name})
	if not isinstance(rows, list):
		raise FrappeCloudError("press.api.site.domains answered without a list")
	return [
		{
			"domain": (row.get("domain") or "").lower(),
			"status": row.get("status") or None,
			"primary": bool(row.get("primary")),
		}
		for row in rows
		if isinstance(row, dict)
	]


def domain_exists(domain: str) -> str | None:
	"""The name of the Frappe Cloud site holding `domain`, on any team, or None."""
	holder = _call("domain_exists", {"domain": domain})
	return holder if isinstance(holder, str) and holder else None


def check_dns(name: str, domain: str) -> bool:
	"""True when Frappe Cloud sees `domain` pointing at the site. Checked live on 2026-11-21: with no record
	pointing there, Frappe Cloud answers HTTP 417 (`DNSValidationError`), not `matched` false."""
	try:
		answer = _call("check_dns", {"name": name, "domain": domain})
	except OutcomeUnknown:
		raise
	except FrappeCloudError as error:
		if error.status == DNS_NOT_MATCHED:
			return False
		raise
	return isinstance(answer, dict) and answer.get("matched") is True


def add_domain(name: str, domain: str):
	_call("add_domain", {"name": name, "domain": domain})


def retry_add_domain(name: str, domain: str):
	_call("retry_add_domain", {"name": name, "domain": domain})


def set_host_name(name: str, domain: str):
	"""Make `domain` the site's primary domain. Safe to repeat."""
	_call("set_host_name", {"name": name, "domain": domain})


def remove_domain(name: str, domain: str):
	_call("remove_domain", {"name": name, "domain": domain})


def _call(method: str, payload: dict, module: str = "site"):
	path = f"press.api.{module}.{method}"
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
		raise FrappeCloudError(f"{path} answered HTTP {response.status_code}", response.status_code)
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
