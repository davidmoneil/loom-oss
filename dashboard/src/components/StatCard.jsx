import WidgetInfo from "./WidgetInfo.jsx";
import { WIDGET_DESCRIPTIONS } from "../widgetDescriptions.js";

// `title` (optional) is a hover tooltip on the value, e.g. the exact
// count behind a compact 1.3B.
export default function StatCard({ label, value, sub, loading, infoKey, title }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-gray-400">
        {label}
        <WidgetInfo text={WIDGET_DESCRIPTIONS[infoKey]} />
      </div>
      {loading ? (
        <div className="skeleton mt-2 h-8 w-24" />
      ) : (
        <div className="mt-1 text-2xl font-semibold text-white" title={title}>
          {value}
        </div>
      )}
      {sub && !loading && (
        <div className="mt-1 text-xs text-gray-500">{sub}</div>
      )}
    </div>
  );
}
