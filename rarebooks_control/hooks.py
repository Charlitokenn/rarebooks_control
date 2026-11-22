app_name = "rarebooks_control"
app_title = "Rarebooks Control"
app_publisher = "RareBooks"
app_description = "RareBooks billing control site: the hosted site registry, payments and pushes"
app_email = ""
app_license = "mit"

# Every whitelisted method must be type annotated, or Frappe refuses it (spec 0008).
require_type_annotated_api_methods = True
# Frappe writes the typing block into each DocType controller on save (developer mode).
export_python_type_annotations = True

# Spec 0008 AC-20: two factor sign in for every desk user. Spec 0010 AC-14: `dormant_days` 0.
after_install = "rarebooks_control.install.after_install"

scheduler_events = {
	"cron": {
		# Spec 0008 AC-13: pushes that are pending, or failing and due.
		"* * * * *": ["rarebooks_control.push.sweep", "rarebooks_control.billing.reconcile_allocations"],
		# Spec 0010 AC-11, AC-16: the config ping and the stale run alert.
		# Spec 0014 AC-10, AC-12, AC-13, AC-17: the claim sweep, the Owner step, the notice retries, the queue
		# alerts and the 2 hour rule. AC-15, AC-16: the build poll and the ping of Ready pool sites.
		"*/5 * * * *": [
			"rarebooks_control.registry.queue_pings",
			"rarebooks_control.onboarding.sweep",
			"rarebooks_control.pool.sweep",
		],
	},
	# Spec 0008 AC-10, AC-16, AC-18: checkout expiry and the ClickPesa reconcile hourly; the PayPal
	# reconcile, the 90 day payload purge and the payment failure digest daily.
	# Spec 0007 AC-21: removal alerts hourly, the approval list and the removal digest daily.
	# Spec 0010 AC-1: the removal round, driven by this site's own scheduler.
	"hourly": [
		"rarebooks_control.reconcile.hourly",
		"rarebooks_control.removal_mail.hourly",
		"rarebooks_control.removal.run_round",
		# Spec 0014 AC-15, AC-16: stale pool sites, then the pool top up.
		"rarebooks_control.pool.hourly",
	],
	"daily": ["rarebooks_control.reconcile.daily", "rarebooks_control.removal_mail.daily"],
}
