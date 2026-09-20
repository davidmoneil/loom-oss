// Central copy for every widget's "i" info icon. Keyed by
// "<page>.<widget>" and looked up via the `infoKey` prop on Chart/StatCard,
// or passed directly to <WidgetInfo text={...}> for custom panels.
export const WIDGET_DESCRIPTIONS = {
  // Overview
  "overview.requests": "Total number of requests in the selected time range.",
  "overview.avgLatency":
    "Average request latency across all providers and models in the selected time range.",
  "overview.costToday": "Total estimated cost of all requests in the selected time range.",
  "overview.tokensInOut":
    "Total input tokens sent to providers and output tokens received back, summed over the selected time range.",
  "overview.activeSessions":
    "Number of distinct sessions with at least one turn in the selected time range.",
  "overview.totalTurns":
    "Total number of conversation turns across all sessions in the selected time range.",
  "overview.requestVolume":
    "Number of requests per time bucket, shown over the selected range.",
  "overview.modelDistribution":
    "Share of requests handled by each model in the selected time range.",
  "overview.tokenFlow":
    "Requested and compressed input tokens (left axis) against output tokens (right axis, scaled independently since output volume is typically much smaller).",
  "overview.cacheHitsByModel":
    "Cached vs. uncached request counts for each model in the selected time range.",
  "overview.providerHealth":
    "Live status of configured upstream providers, plus whether routing, compression, and detection are enabled.",
  "overview.compression":
    "Token savings from prompt compression in the selected time range, broken down by content block type.",

  // Metrics
  "metrics.costOverTime": "Estimated cost per time bucket, shown over the selected range.",
  "metrics.tokenUsageOverTime":
    "Input and output tokens per time bucket, stacked to show total volume over the selected range.",
  "metrics.requestsByTaskType":
    "Request counts grouped by the task type classification assigned to each request.",
  "metrics.distributionBySource":
    "Share of requests (or cost, when available) attributed to each request source.",

  // Costs
  "costs.totalCost": "Total estimated spend across all requests in the selected range.",
  "costs.requests": "Total number of requests in the selected range.",
  "costs.tokensIn": "Total input tokens sent to providers in the selected range.",
  "costs.tokensOut": "Total output tokens received from providers in the selected range.",
  "costs.tokensSaved": "Total tokens removed by compression before being sent to providers.",
  "costs.savings": "Estimated cost avoided by compressing requests before sending them to providers.",
  "costs.costByDay": "Estimated daily spend over the selected range.",
  "costs.costByModel": "Estimated spend broken down by model.",
  "costs.dailyRequestVolume": "Number of requests per day over the selected range.",
  "costs.distributionBySource":
    "Share of cost (or requests, when cost data is unavailable) attributed to each request source.",

  // Compression
  "compression.tokensSaved": "Total tokens removed by compression before being sent to providers.",
  "compression.estSavings":
    "Estimated cost avoided by compressing requests, based on the tokens saved.",
  "compression.meanSavings":
    "Average percentage reduction in token count per compressed request, with median and standard deviation.",
  "compression.requestsCompressed":
    "Share of requests that were compressed before being sent to a provider.",
  "compression.tokensSavedByDay": "Tokens saved by compression per day over the selected range.",
  "compression.savingsDistribution":
    "Distribution of per-request compression savings, bucketed by percentage saved.",
  "compression.tokensSavedByModel": "Tokens saved by compression, broken down by model.",
  "compression.compressedVsTotal":
    "Total requests compared against the subset that were compressed, per day.",
  "compression.byTier": "Compression outcomes broken down by compression tier.",
  "compression.bySource": "Compression outcomes broken down by request source.",

  // Routing
  "routing.totalDecisions": "Total number of routing decisions recorded in the selected time range.",
  "routing.uniqueModels": "Number of distinct models that requests were routed to.",
  "routing.overrides":
    "Number of routing decisions where the model actually used differed from the recommended model.",
  "routing.routingReasons": "Number of distinct reasons recorded for routing decisions.",
  "routing.decisionsByModel": "Share of routing decisions that resulted in each model being used.",
  "routing.routingReasonsChart":
    "Count of routing decisions grouped by the reason recorded for each decision.",
  "routing.recentDecisions":
    "The most recent individual routing decisions, newest first, capped to the last 100.",
  "routing.table":
    "Empirical routing table entries — per model/task-type/temperature combination, with EQRT (determinism, lexical, structural, semantic, exact-match) scores from evaluation runs.",
  "routing.modelAssignment":
    "Restrict which models are eligible for each source policy. Toggle models on to build an allow-list; with no models selected, all configured models remain eligible.",

  // Sessions
  "sessions.sessions": "Number of distinct sessions in the selected time range.",
  "sessions.totalTurns": "Total number of conversation turns across all sessions in the selected time range.",
  "sessions.totalCost": "Total estimated cost across all requests in the selected time range.",
  "sessions.avgTurnsPerSession": "Average number of turns per session in the selected time range.",
  "sessions.turnsPerSession": "Number of turns recorded for each individual session.",
  "sessions.requestsByModel": "Request counts broken down by model across all sessions.",

  // Rate limits
  "rateLimits.currentStatus": "Most recently reported rate-limit status from the active provider.",
  "rateLimits.retryAfter":
    "Retry-after duration returned by the provider on the most recent rate-limit response, if any.",
  "rateLimits.model": "Model and source associated with the most recent rate-limit reading.",
  "rateLimits.lastSeen": "Timestamp of the most recent rate-limit reading.",
  "rateLimits.currentUtilization":
    "Current usage against the provider's 5-hour and 7-day rate-limit windows.",
  "rateLimits.perModelUtilization":
    "Current usage against the provider's rate-limit window for the specific model in use, when reported separately.",
  "rateLimits.utilizationTrend":
    "5-hour and 7-day average utilization over time, with peak 5-hour utilization shown for reference.",
};
