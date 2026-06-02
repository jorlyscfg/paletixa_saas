app_name = "paletixa_saas"
app_title = "Paletixa SaaS"
app_publisher = "Jegdev"
app_description = "SaaS Platform for La Paletixa"
app_email = "admin@lapaletixa.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "paletixa_saas",
# 		"logo": "/assets/paletixa_saas/logo.png",
# 		"title": "Paletixa SaaS",
# 		"route": "/paletixa_saas",
# 		"has_permission": "paletixa_saas.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/paletixa_saas/css/paletixa_saas.css"
# app_include_js = "/assets/paletixa_saas/js/paletixa_saas.js"

# include js, css files in header of web template
# web_include_css = "/assets/paletixa_saas/css/paletixa_saas.css"
# web_include_js = "/assets/paletixa_saas/js/paletixa_saas.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "paletixa_saas/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "paletixa_saas/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "paletixa_saas.utils.jinja_methods",
# 	"filters": "paletixa_saas.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "paletixa_saas.install.before_install"
# after_install = "paletixa_saas.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "paletixa_saas.uninstall.before_uninstall"
# after_uninstall = "paletixa_saas.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "paletixa_saas.utils.before_app_install"
# after_app_install = "paletixa_saas.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "paletixa_saas.utils.before_app_uninstall"
# after_app_uninstall = "paletixa_saas.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "paletixa_saas.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "paletixa_saas.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"paletixa_saas.tasks.all"
# 	],
# 	"daily": [
# 		"paletixa_saas.tasks.daily"
# 	],
# 	"hourly": [
# 		"paletixa_saas.tasks.hourly"
# 	],
# 	"weekly": [
# 		"paletixa_saas.tasks.weekly"
# 	],
# 	"monthly": [
# 		"paletixa_saas.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "paletixa_saas.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "paletixa_saas.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "paletixa_saas.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "paletixa_saas.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["paletixa_saas.utils.before_request"]
# after_request = ["paletixa_saas.utils.after_request"]

# Job Events
# ----------
# before_job = ["paletixa_saas.utils.before_job"]
# after_job = ["paletixa_saas.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"paletixa_saas.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


doc_events = {
	"Item": {
		"before_save": "paletixa_saas.paletixa_saas.api.clean_old_image_file"
	},
	"Sales Order": {
		"after_insert": "paletixa_saas.paletixa_saas.api.create_notification_on_order"
	}
}
