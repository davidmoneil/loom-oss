import { useEffect, useState, useCallback } from "react";
import { Header } from "./Overview.jsx";
import { api } from "../api.js";

// Request tags: shows the configured header -> tag mapping, the header NAMES
// (never values) seen recently so you can pick new ones, and spend grouped by
// tag. Credential headers are never captured or listed by the gateway.
export default function Tags() {
  const [tags, setTags] = useState(null);
  const [series, setSeries] = useState(null);
  const [hours, setHours] = useState(24);
  const [error, setError] = useState(null);
  const [updatedAt, setUpdatedAt] = useState(null);

  const load = useCallback(async () => {
    try {
      const [t, s] = await Promise.all([api.requestTags(hours), api.timeseries(hours, "1h")]);
      setTags(t);
      setSeries(s);
      setUpdatedAt(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, [hours]);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const byTag = series?.by_tag || {};
  const rows = Object.entries(byTag).flatMap(([name, values]) =>
    Object.entries(values).map(([value, v]) => ({ name, value, ...v }))
  );
  rows.sort((a, b) => b.cost - a.cost);

  return (
    <div className="p-6">
      <Header title="Request Tags" updatedAt={updatedAt} error={error} onRefresh={load}>
        <select
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          className="rounded-md border border-border bg-card px-3 py-2 text-sm text-gray-200"
        >
          <option value={1}>1h</option>
          <option value={24}>24h</option>
          <option value={168}>7d</option>
        </select>
      </Header>

      {tags && !tags.enabled && (
        <div className="mt-6 rounded-lg border border-border bg-card p-6 text-sm text-gray-400">
          Request tags are off. Enable <code>request_tags</code> in the gateway config and list the
          headers to capture (see <code>loom.example.yaml</code>).
        </div>
      )}

      {tags?.enabled && (
        <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="rounded-lg border border-border bg-card p-4">
            <h3 className="mb-3 text-sm font-semibold text-gray-200">Header names seen ({hours}h)</h3>
            <p className="mb-3 text-xs text-gray-500">Names and counts only. Never values.</p>
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-gray-400">
                <tr><th className="py-1">Header</th><th>Requests</th><th>Tag</th></tr>
              </thead>
              <tbody>
                {(tags.seen_headers || []).map((h) => (
                  <tr key={h.name} className="border-t border-border">
                    <td className="py-1 font-mono text-gray-200">{h.name}</td>
                    <td className="text-gray-300">{h.count}</td>
                    <td className="text-gray-300">{h.tag || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(tags.seen_headers || []).length === 0 && (
              <div className="py-4 text-sm text-gray-500">No requests seen yet.</div>
            )}
          </div>

          <div className="rounded-lg border border-border bg-card p-4">
            <h3 className="mb-3 text-sm font-semibold text-gray-200">Usage by tag ({hours}h)</h3>
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-gray-400">
                <tr><th className="py-1">Tag</th><th>Value</th><th>Requests</th><th>Cost</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.name}:${r.value}`} className="border-t border-border">
                    <td className="py-1 text-gray-300">{r.name}</td>
                    <td className="font-mono text-gray-200">{r.value}</td>
                    <td className="text-gray-300">{r.requests}</td>
                    <td className="text-gray-300">${r.cost.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && <div className="py-4 text-sm text-gray-500">No tagged requests yet.</div>}
          </div>
        </div>
      )}
    </div>
  );
}
