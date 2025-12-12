# Patch ERPNext functions to handle missing customers/suppliers during sync
from havano_sync.havano_sync.utils.party import patch_payment_terms_template

# Apply patches when module is imported
patch_payment_terms_template()

