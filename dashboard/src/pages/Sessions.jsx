import { useEffect, useState, useCallback } from "react";
import StatCard from "../components/StatCard.jsx";
import { Header } from "./Overview.jsx";
import { api, fmtNumber, fmtTime } from "../api.js";

const RANGES = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
];

export default function Sessions() {
  const [range, setRange] = useState(RANGES[2]);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.sessions(range.hours);
      setData(d);
      setUpdatedAt(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [range]);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const entries = data?.entries || [];
  const sources = {};
  let totalTurns = 0;
  entries.forEach((e) => {
    sources[e.source] = (sources[e.source] || 0) + 1;
    totalTurns += e.turns || 0;
  });
  const sourceList = Object.entries(sources).sort((a, b) => b[1] - a[1]);

  return (
    <div className="p-6">
      <Header title="Sessions" updatedAt={updatedAt} error={error} onRefresh={load}>
        <div className="flex gap-1 rounded-md border border-border bg-card p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.label}
              onClick={() => setRange(r)}
              className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                r.label === range.label
                  ? "bg-accent text-white"
                  : "text-gray-400 hover:text-white"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </Header>

      {!data?.supported && !loading && (
        <div className="mt-6 rounded-lg border border-border bg-card p-8 text-center text-sm text-gray-400">
          Session tracking requires storage to be enabled in the gateway config.
        </div>
      )}

      {data?.supported && (
        <>
          <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatCard label="Active Sessions" value={fmtNumber(entries.length)} loading={loading} />
            <StatCard label="Total Turns" value={fmtNumber(totalTurns)} loading={loading} />
            <StatCard label="Unique Sources" value={fmtNumber(sourceList.length)} loading={loading} />
            <StatCard
              label="Avg Turns/Session"
              value={entries.length > 0 ? (totalTurns / entries.length).toFixed(1) : "—"}
              loading={loading}
            />
          </div>

          <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="rounded-lg border border-border bg-card p-4">
              <h3 className="mb-3 text-sm font-semibold text-gray-200">By source</h3>
              {sourceList.length === 0 ? (
                <div className="text-sm text-gray-500">No sessions</div>
              ) : (
                <div className="space-y-2">
                  {sourceList.map(([source, count]) => (
                    <div key={source} className="flex items-center justify-between">
                      <span className="font-mono text-sm text-gray-300">{source}</span>
                      <span className="rounded-full bg-gray-700 px-2 py-0.5 text-xs font-medium text-gray-300">
                        {count}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="lg:col-span-2 rounded-lg border border-border bg-card p-4">
              <h3 className="mb-3 text-sm font-semibold text-gray-200">Recent sessions</h3>
              {loading ? (
                <div className="skeleton h-48 w-full" />
              ) : entries.length === 0 ? (
                <div className="text-sm text-gray-500">No sessions in this window</div>
              ) : (
                <div className="max-h-96 overflow-y-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="sticky top-0 bg-card">
                      <tr className="border-b border-border text-xs font-medium uppercase text-gray-400">
                        <th className="pb-2 pr-3">Session</th>
                        <th className="pb-2 pr-3">Source</th>
                        <th className="pb-2 pr-3 text-right">Turns</th>
                        <th className="pb-2 text-right">Last Seen</th>
                      </tr>
                    </thead>
                    <tbody>
                      {entries.map((s) => (
                        <tr key={s.session_id} className="border-b border-border/50">
                          <td className="py-2 pr-3 font-mono text-xs text-gray-300">
                            {s.session_id?.slice(0, 12) ?? "—"}
                          </td>
                          <td className="py-2 pr-3 text-gray-300">{s.source}</td>
                          <td className="py-2 pr-3 text-right text-gray-300">{s.turns}</td>
                          <td className="py-2 text-right text-xs text-gray-400">{fmtTime(s.last_seen)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
