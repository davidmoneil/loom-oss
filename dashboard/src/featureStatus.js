// Feature maturity registry, keyed by route path.
//
// Each entry drives two things automatically (no per-page wiring):
//   1. a status chip next to the item in the sidebar (Layout.jsx)
//   2. an explanatory banner at the top of the page (FeatureStatusBanner.jsx)
//
// When a feature ships, delete its entry here and close the linked issue.
export const FEATURE_STATUS = {
  '/governor': {
    badge: 'In Planning',
    summary:
      'Budgets and limits can be configured and spend is tracked live, but enforcement ' +
      '(warn / throttle / block when a limit is hit) is not implemented yet.',
    issue: 'https://github.com/davidmoneil/loom-oss/issues/32',
  },
  '/rate-limits': {
    badge: 'In Planning',
    summary:
      'Provider rate-limit state is captured and displayed live. Editing thresholds and ' +
      'alert levels from the dashboard is planned — today this page is read-only.',
    issue: 'https://github.com/davidmoneil/loom-oss/issues/33',
  },
  '/routing': {
    badge: 'In Planning',
    summary:
      'Routing decisions and tier distribution are shown live. Editing routing rules from ' +
      'the dashboard is planned — today rules are configured in loom.yaml.',
    issue: 'https://github.com/davidmoneil/loom-oss/issues/34',
  },
  '/scanner': {
    badge: 'Partial',
    summary:
      'Detection and redaction run in the gateway and settings can be toggled here. Full ' +
      'rule management (add / edit / test rules in the UI) is planned.',
    issue: 'https://github.com/davidmoneil/loom-oss/issues/35',
  },
}
