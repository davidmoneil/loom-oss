import { useLocation } from 'react-router-dom'
import { FEATURE_STATUS } from '../featureStatus'

// Renders an explanatory banner when the current route has an entry in
// FEATURE_STATUS. Mounted once in Layout, above the page outlet.
export default function FeatureStatusBanner() {
  const { pathname } = useLocation()
  const status = FEATURE_STATUS[pathname]
  if (!status) return null

  return (
    <div className="mx-6 mt-4 flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm">
      <span className="mt-0.5 shrink-0 rounded bg-amber-500/20 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-amber-400">
        {status.badge}
      </span>
      <div className="min-w-0">
        <p className="text-gray-300">{status.summary}</p>
        <a
          href={status.issue}
          target="_blank"
          rel="noreferrer"
          className="mt-1 inline-block text-amber-400 hover:underline"
        >
          Follow progress on the tracked issue →
        </a>
      </div>
    </div>
  )
}
