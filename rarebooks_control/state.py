"""A hosted site's billing state, the six fields every push and pull carries, and its date rules (spec 0008)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from frappe.utils import getdate

PUSH_FIELDS = ("paid_until", "provider", "billing_period", "auto_renews", "provider_reference", "exempt")
ONE_DAY = timedelta(days=1)


def site_state(row) -> dict:
	"""The six pushed fields, in the form the customer site checks (spec 0005)."""
	paid_until = row.paid_until
	return {
		"paid_until": str(getdate(paid_until)) if paid_until else None,
		"provider": row.provider or None,
		"billing_period": row.billing_period or None,
		"auto_renews": int(row.auto_renews or 0),
		"provider_reference": row.provider_reference or None,
		"exempt": int(row.exempt or 0),
	}


def site_zone(time_zone) -> ZoneInfo:
	"""The site's time zone; UTC when empty or not a valid IANA name, never an error (spec 0005 AC-18)."""
	if not isinstance(time_zone, str) or not time_zone:
		return ZoneInfo("UTC")
	try:
		return ZoneInfo(time_zone)
	except (ZoneInfoNotFoundError, ValueError):
		return ZoneInfo("UTC")


def site_date(utc_moment: datetime, time_zone) -> date:
	"""The site's day at a naive UTC moment."""
	return utc_moment.replace(tzinfo=UTC).astimezone(site_zone(time_zone)).date()


def clickpesa_paid_until(old_paid_until, paid_on: datetime, trial_ends_on, time_zone) -> date:
	"""One year from the latest of the day after the old end, the payment day in the site's zone, and the day
	after the trial; empty terms are left out (AC-8). Never earlier than the old end."""
	starts = [site_date(paid_on, time_zone)]
	if old_paid_until:
		starts.append(getdate(old_paid_until) + ONE_DAY)
	if trial_ends_on:
		starts.append(getdate(trial_ends_on) + ONE_DAY)
	start = max(starts)
	# relativedelta keeps 29 February on 28 February, so the year then ends on 27 February.
	return start + relativedelta(years=1) - ONE_DAY
