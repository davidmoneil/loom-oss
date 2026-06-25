export default function StatCard({ label, value, sub, loading }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-gray-400">
        {label}
      </div>
      {loading ? (
        <div className="skeleton mt-2 h-8 w-24" />
      ) : (
        <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
      )}
      {sub && !loading && (
        <div className="mt-1 text-xs text-gray-500">{sub}</div>
      )}
    </div>
  );
}
