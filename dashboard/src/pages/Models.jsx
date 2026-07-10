import { useEffect, useState, useCallback } from "react";
import { Header } from "./Overview.jsx";
import { api } from "../api.js";

const TIER_COLORS = {
  economy: "bg-green-500/20 text-green-400 border-green-500/30",
  standard: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  premium: "bg-purple-500/20 text-purple-400 border-purple-500/30",
};

function fmtCtx(tokens) {
  if (!tokens) return "—";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(0)}M`;
  return `${(tokens / 1000).toFixed(0)}k`;
}

function fmtRate(cost) {
  if (cost === 0 || cost == null) return "free";
  if (cost < 0.001) return `$${cost.toFixed(5)}`;
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(3)}`;
}

export default function Models() {
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.models();
      setModels(res.data || []);
      setUpdatedAt(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = filter
    ? models.filter(
        (m) =>
          m.id.toLowerCase().includes(filter.toLowerCase()) ||
          (m.display_name || "").toLowerCase().includes(filter.toLowerCase()) ||
          m.provider.toLowerCase().includes(filter.toLowerCase()) ||
          m.tier.toLowerCase().includes(filter.toLowerCase())
      )
    : models;

  const providers = [...new Set(models.map((m) => m.provider))];
  const tiers = [...new Set(models.map((m) => m.tier))];

  return (
    <div className="p-6">
      <Header title="Models" updatedAt={updatedAt} error={error} onRefresh={load}>
        <input
          type="text"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter models..."
          className="rounded-md border border-border bg-card px-3 py-1.5 text-sm text-white placeholder-gray-500 focus:border-accent focus:outline-none"
        />
      </Header>

      {/* Summary cards */}
      <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-gray-400">
            Total Models
          </div>
          {loading ? (
            <div className="skeleton mt-2 h-8 w-16" />
          ) : (
            <div className="mt-1 text-2xl font-semibold text-white">
              {models.length}
            </div>
          )}
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-gray-400">
            Providers
          </div>
          {loading ? (
            <div className="skeleton mt-2 h-8 w-16" />
          ) : (
            <>
              <div className="mt-1 text-2xl font-semibold text-white">
                {providers.length}
              </div>
              <div className="mt-1 text-xs text-gray-500">
                {providers.join(", ")}
              </div>
            </>
          )}
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-gray-400">
            Tool Support
          </div>
          {loading ? (
            <div className="skeleton mt-2 h-8 w-16" />
          ) : (
            <>
              <div className="mt-1 text-2xl font-semibold text-white">
                {models.filter((m) => m.supports_tools).length}/{models.length}
              </div>
              <div className="mt-1 text-xs text-gray-500">models with tool use</div>
            </>
          )}
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-gray-400">
            Tiers
          </div>
          {loading ? (
            <div className="skeleton mt-2 h-8 w-16" />
          ) : (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {tiers.map((t) => (
                <span
                  key={t}
                  className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${TIER_COLORS[t] || "bg-gray-700/50 text-gray-400 border-border"}`}
                >
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Models table */}
      <div className="mt-6 overflow-x-auto rounded-lg border border-border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs font-semibold uppercase tracking-wider text-gray-400">
              <th className="px-4 py-3">Model</th>
              <th className="px-4 py-3">Provider</th>
              <th className="px-4 py-3">Tier</th>
              <th className="px-4 py-3 text-right">Input /1k</th>
              <th className="px-4 py-3 text-right">Output /1k</th>
              <th className="px-4 py-3 text-right">Context</th>
              <th className="px-4 py-3 text-center">Tools</th>
              <th className="px-4 py-3 text-center">JSON</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 4 }).map((_, i) => (
                <tr key={i} className="border-b border-border/50">
                  {Array.from({ length: 8 }).map((_, j) => (
                    <td key={j} className="px-4 py-3">
                      <div className="skeleton h-5 w-20" />
                    </td>
                  ))}
                </tr>
              ))
            ) : filtered.length === 0 ? (
              <tr>
                <td
                  colSpan={8}
                  className="px-4 py-12 text-center text-gray-500"
                >
                  {models.length === 0
                    ? "No models configured"
                    : "No models match your filter"}
                </td>
              </tr>
            ) : (
              filtered.map((m) => (
                <tr
                  key={m.id}
                  className="border-b border-border/50 transition-colors hover:bg-gray-800/30"
                >
                  <td className="px-4 py-3">
                    <div className="font-mono text-sm text-white">{m.id}</div>
                    {m.display_name && m.display_name !== m.id && (
                      <div className="text-xs text-gray-500">
                        {m.display_name}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-300">{m.provider}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${TIER_COLORS[m.tier] || "bg-gray-700/50 text-gray-400 border-border"}`}
                    >
                      {m.tier}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-gray-300">
                    {fmtRate(m.cost_per_1k_input)}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-gray-300">
                    {fmtRate(m.cost_per_1k_output)}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-gray-300">
                    {fmtCtx(m.max_context_tokens)}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {m.supports_tools ? (
                      <span className="text-green-400">&#10003;</span>
                    ) : (
                      <span className="text-gray-600">&#10005;</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {m.supports_json_mode ? (
                      <span className="text-green-400">&#10003;</span>
                    ) : (
                      <span className="text-gray-600">&#10005;</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
