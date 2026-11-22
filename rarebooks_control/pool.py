"""The warm pool of Books sites (spec 0014 AC-15, AC-16).

The control site keeps `pool_size` sites built on Frappe Cloud, with Books installed and their config working,
plus one for each Queued sign up. A build first commits its Books Hosted Site row (`pool_status` Building), then
asks Frappe Cloud for the site, so no Frappe Cloud site exists without a row. The 5 minute sweep sends a Building
site its config once Frappe Cloud shows it Active, and the first verified `control_ping` makes it Ready
(`registry.ping_pending`). A site that never gets there, goes silent, or grows old is discarded through a Discard
removal case (AC-19, `removal.open_discard`) and replaced by the next top up. Two failed builds in a row pause
the builds until a Billing Manager clears the pause.

No Frappe Cloud call or site call is made with a row lock held (spec 0007 lock hardening): the top up counts and
inserts under the settings row lock, commits, then calls.
"""

import secrets
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime

from rarebooks_control import registry, removal, removal_mail
from rarebooks_control.providers import frappe_cloud
from rarebooks_control.providers.frappe_cloud import FrappeCloudError, OutcomeUnknown
from rarebooks_control.signing import utc_now

SITE = "Books Hosted Site"
SIGNUP = "Books Signup"
SETTINGS = "Books Onboarding Settings"
REMOVAL_SETTINGS = "Books Removal Settings"
TOP_UP_JOB = "rarebooks_control.pool.top_up"
APPS = ["frappe", "frappe_books"]
BUILDS_PER_RUN = 3
# A Building site not Ready this long after `pool_built_on` is discarded (AC-16).
BUILD_WAIT = timedelta(hours=1)
# A Ready site whose `control_ping` fails this long is discarded (AC-16).
SILENT_WAIT = timedelta(hours=1)
# A Ready site older than this by `pool_built_on` is discarded and rebuilt (AC-16).
STALE_AGE = timedelta(days=30)
# This many failed builds in a row pause the builds (AC-16).
PAUSE_AT = 2
SILENT_KEY_SECONDS = 2 * 24 * 60 * 60


def hourly():
	"""The hourly job: discard stale Ready sites, then top the pool up (AC-15, AC-16)."""
	for name in _pool_sites("Ready"):
		_isolated(discard_if_stale, name, "the stale check")
	_isolated(lambda _name: top_up(), "pool", "the top up")


def sweep():
	"""The 5 minute job: send config to Building sites Frappe Cloud shows Active, end builds over their hour, and
	ping Ready sites so a silent one is found (AC-15, AC-16)."""
	for name in _pool_sites("Building"):
		_isolated(poll_building, name, "the build poll")
	for name in _pool_sites("Ready"):
		_isolated(check_silent, name, "the pool ping")


def request_build():
	"""No Ready site for a sign up: top the pool up at once rather than at the next hour (AC-11, AC-15)."""
	frappe.enqueue(
		TOP_UP_JOB, queue="short", job_id="pool-top-up", deduplicate=True, enqueue_after_commit=True
	)


# The top up (AC-15)


def top_up():
	"""Start builds until Ready plus Building sites equal `pool_size` plus the Queued sign ups, at most 3 a run,
	and none while builds are paused. Counted and inserted under the settings row lock, so two top ups can't both
	build the same gap; the Frappe Cloud calls follow the commit."""
	settings = frappe.db.get_singles_dict(SETTINGS, for_update=True)
	if cint(settings.get("pool_builds_paused")):
		frappe.db.commit()
		return []
	missing = _missing_settings(settings)
	if missing:
		frappe.db.commit()
		if removal_mail._first_alert(f"pool-settings:{utc_now().date()}"):
			frappe.log_error(f"Pool: no builds until these are set: {', '.join(missing)}")
		return []
	queued = frappe.db.count(SIGNUP, {"status": "Queued"})
	wanted = cint(settings.get("pool_size")) + queued - pool_count()
	names = [
		_insert_building(settings.get("frappe_cloud_domain")) for _build in range(min(wanted, BUILDS_PER_RUN))
	]
	# Committed before any `new_site`, so no Frappe Cloud site exists without its row (key invariants).
	frappe.db.commit()
	for name in names:
		_isolated(build, name, "the build")
	return names


def pool_count() -> int:
	"""Ready plus Building sites, less those already being discarded, so a dying site is replaced."""
	return frappe.db.sql(
		"""select count(*) from `tabBooks Hosted Site` site
		where site.status = 'Active' and site.pool_status in ('Building', 'Ready')
		and not exists (
			select 1 from `tabBooks Removal Case` c
			where c.site = site.name and c.status not in ('Removed', 'Cancelled')
		)"""
	)[0][0]


def _insert_building(domain: str) -> str:
	name = f"rb-{secrets.token_hex(4)}.{domain}"
	frappe.get_doc(
		{
			"doctype": SITE,
			"site": name,
			"site_url": f"https://{name}",
			"status": "Active",
			"config_status": "Pending",
			"control_secret": secrets.token_hex(32),
			"pool_status": "Building",
			"pool_built_on": utc_now(),
		}
	).insert(ignore_permissions=True)
	return name


def build(name: str):
	"""Ask Frappe Cloud for the site of a committed Building row. A clear refusal means no site was made: the row
	is deleted and the failure counted. An unknown outcome keeps the row: the poll finds the site, or the hour
	ends it (AC-15, AC-16)."""
	row = frappe.db.get_value(SITE, name, ["pool_status", "status", "config_sent_on"], as_dict=True)
	if not row or row.pool_status != "Building" or row.status != "Active" or row.config_sent_on:
		return
	if frappe.db.get_single_value(SETTINGS, "pool_builds_paused"):
		# An earlier build of this run may have paused the builds (AC-16): none starts while paused (AC-15), and
		# no site was made for this row.
		frappe.delete_doc(SITE, name, ignore_permissions=True, force=True, delete_permanently=True)
		return
	label, domain = name.split(".", 1)
	group = frappe.db.get_single_value(REMOVAL_SETTINGS, "books_bench_group")
	plan = frappe.db.get_single_value(SETTINGS, "frappe_cloud_plan")
	try:
		answered = frappe_cloud.new_site(label, domain, group, plan, APPS)
	except OutcomeUnknown as error:
		frappe.log_error(f"Pool: the build of {name} was sent with no clear answer", str(error))
		return
	except FrappeCloudError as error:
		frappe.delete_doc(SITE, name, ignore_permissions=True, force=True, delete_permanently=True)
		count_failure(_("Frappe Cloud refused the build of {0}: {1}").format(name, str(error)))
		return
	if answered != name:
		frappe.log_error(f"Pool: Frappe Cloud named the build {answered}, not {name}")


# From Building to Ready (AC-15)


def poll_building(name: str):
	"""A Building site gets its config once Frappe Cloud shows it Active on the Books bench group, under spec
	0010 AC-10's checks. Over its hour, it is discarded and counts one failed build (AC-16)."""
	row = frappe.get_doc(SITE, name)
	if row.pool_status != "Building" or row.status != "Active" or _being_discarded(name):
		return
	if utc_now() - get_datetime(row.pool_built_on) >= BUILD_WAIT:
		removal.open_discard(name, _("The build of {0} wasn't Ready within an hour.").format(name))
		count_failure(
			_("The build of {0} wasn't Ready within an hour of {1} UTC.").format(name, row.pool_built_on)
		)
		return
	if row.config_sent_on:
		# The ping in `registry.run_pings` makes it Ready.
		return
	group = frappe.db.get_single_value(REMOVAL_SETTINGS, "books_bench_group")
	try:
		cloud = frappe_cloud.get_site(name)
	except FrappeCloudError:
		return
	if (
		not group
		or cloud["name"] != name
		or cloud["group"] != group
		or cloud["status"] != frappe_cloud.ACTIVE
	):
		return
	if not registry.send_config(name, row.get_password("control_secret")):
		return
	row = frappe.get_doc(SITE, name, for_update=True)
	if row.pool_status == "Building" and not row.config_sent_on:
		frappe.db.set_value(SITE, name, "config_sent_on", utc_now())


def became_ready():
	"""A build reached Ready: the failure count starts again (AC-16). Called in the ping's transaction."""
	if cint(frappe.db.get_single_value(SETTINGS, "build_failures")):
		frappe.db.set_single_value(SETTINGS, "build_failures", 0)


def count_failure(reason: str):
	"""One failed build. The second in a row pauses the builds, with the reason and one ops email (AC-16)."""
	settings = frappe.db.get_singles_dict(SETTINGS, for_update=True)
	failures = cint(settings.get("build_failures")) + 1
	frappe.db.set_single_value(SETTINGS, "build_failures", failures)
	if failures < PAUSE_AT or cint(settings.get("pool_builds_paused")):
		return
	frappe.db.set_single_value(SETTINGS, {"pool_builds_paused": 1, "pool_pause_reason": reason})
	removal_mail.ops_alert(
		_("Pool builds are paused after {0} failed builds").format(failures),
		[
			reason,
			_("No new pool site is built until you clear Pool builds paused in Books Onboarding Settings."),
			_("Sign ups wait Queued while no Ready site is left."),
		],
	)


# Ready sites that go silent or grow old (AC-16)


def check_silent(name: str):
	"""Ping a Ready site. The first failure is noted; a site still failing an hour later is discarded."""
	row = frappe.get_doc(SITE, name)
	if row.pool_status != "Ready" or row.status != "Active" or _being_discarded(name):
		return
	key = f"pool-silent:{name}"
	if registry._pinged(row, row.get_password("control_secret", raise_exception=False)):
		frappe.cache.delete_value(key)
		return
	first = frappe.cache.get_value(key)
	if not first:
		frappe.cache.set_value(key, utc_now().isoformat(), expires_in_sec=SILENT_KEY_SECONDS)
		return
	if utc_now() - datetime.fromisoformat(first) >= SILENT_WAIT:
		frappe.cache.delete_value(key)
		removal.open_discard(
			name, _("The pool site {0} hasn't answered a ping since {1} UTC.").format(name, first)
		)


def discard_if_stale(name: str):
	row = frappe.db.get_value(SITE, name, ["pool_status", "status", "pool_built_on"], as_dict=True)
	if not row or row.pool_status != "Ready" or row.status != "Active" or _being_discarded(name):
		return
	if row.pool_built_on and utc_now() - get_datetime(row.pool_built_on) >= STALE_AGE:
		removal.open_discard(name, _("The pool site {0} is older than 30 days.").format(name))


# Shared


def _pool_sites(pool_status: str) -> list[str]:
	return frappe.get_all(
		SITE, filters={"status": "Active", "pool_status": pool_status}, pluck="name", order_by="pool_built_on"
	)


def _being_discarded(site: str) -> bool:
	return bool(removal.open_case(site))


def _missing_settings(settings: dict) -> list[str]:
	missing = [field for field in ("frappe_cloud_plan", "frappe_cloud_domain") if not settings.get(field)]
	if not frappe.db.get_single_value(REMOVAL_SETTINGS, "books_bench_group"):
		missing.append("books_bench_group")
	if not registry.control_public_url():
		missing.append("books_control_public_url")
	missing += [key for key in frappe_cloud.CONF_KEYS if not frappe.conf.get(key)]
	return missing


def _isolated(task, name: str, what: str):
	"""One site at a time: a failure is logged and rolled back, and the others still run."""
	try:
		task(name)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(f"Pool: {what} of {name} failed")
