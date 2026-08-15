"""Provider usage reconciliation — compare Loom-tracked usage against
provider-reported usage (Anthropic, OpenAI) to surface shadow traffic.

Part of AIProjects-8rl7. This package holds one ingestion adapter per
provider; each adapter fetches usage from the provider's admin/usage API
and normalizes it into `ProviderUsageRecord` for the comparison engine
(AIProjects-srdn) to consume.
"""

from .models import ProviderUsageRecord

__all__ = ["ProviderUsageRecord"]
