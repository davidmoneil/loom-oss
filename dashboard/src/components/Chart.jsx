import { ResponsiveContainer } from "recharts";

// Card wrapper around a chart with a title, fixed height, and graceful
// loading / empty states.
export default function Chart({ title, loading, empty, height = 280, children }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-semibold text-gray-200">{title}</h3>
      {loading ? (
        <div className="skeleton" style={{ height }} />
      ) : empty ? (
        <div
          className="flex items-center justify-center text-sm text-gray-500"
          style={{ height }}
        >
          No data yet
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          {children}
        </ResponsiveContainer>
      )}
    </div>
  );
}

export const CHART_COLORS = [
  "#3b82f6",
  "#10b981",
  "#f59e0b",
  "#8b5cf6",
  "#ef4444",
  "#06b6d4",
  "#ec4899",
  "#84cc16",
];

export const axisProps = {
  stroke: "#6b7280",
  tick: { fill: "#9ca3af", fontSize: 12 },
};

export const tooltipStyle = {
  contentStyle: {
    background: "#1f2937",
    border: "1px solid #374151",
    borderRadius: 8,
    color: "#e5e7eb",
  },
  labelStyle: { color: "#9ca3af" },
};
