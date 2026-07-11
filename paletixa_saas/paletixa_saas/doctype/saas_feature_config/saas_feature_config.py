from frappe.model.document import Document

from paletixa_saas.config.platform_defaults import _validate_platform_distribution_warehouse


class SaaSFeatureConfig(Document):
	def validate(self):
		self.default_distribution_warehouse = _validate_platform_distribution_warehouse(
			self.get("default_distribution_warehouse"),
			company_name=self.get("company_name"),
		)
