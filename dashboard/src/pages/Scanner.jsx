import { useEffect, useState, useCallback } from "react";
import StatCard from "../components/StatCard.jsx";
import { api, fmtNumber } from "../api.js";

const ACTION_OPTIONS = ["redact", "mask", "pseudonymize", "log_only", "pass"];

export default function Scanner() {
  const [data, setData] = useState(null);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState(null);

  const refresh = useCallback(() => {
    Promise.all([
      api.scannerRules(),
      api.scannerStats(),
    ])
      .then(([rulesData, statsData]) => {
        setData(rulesData);
        setStats(statsData);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
  }, [refresh]);

  async function toggleRule(name, enabled) {
    setUpdating(name);
    try {
      await api.updateScannerRule(name, { enabled });
      refresh();
    } catch (e) {
      console.error("Failed to update rule:", e);
    } finally {
      setUpdating(null);
    }
  }

  async function changeAction(name, action) {
    setUpdating(name);
    try {
      await api.updateScannerRule(name, { action });
      refresh();
    } catch (e) {
      console.error("Failed to update rule:", e);
    } finally {
      setUpdating(null);
    }
  }

  if (loading) {
    return (
      <div className="p-6">
        <h1 className="mb-6 text-2xl font-bold text-white">Data Protection</h1>
        <div className="animate-pulse text-gray-400">Loading scanner config...</div>
      </div>
    );
  }

  const rules = data?.rules || [];
  const skipConfig = data?.skip_config || {};
  const enabled = data?.enabled || false;

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Data Protection</h1>
        <div className="flex items-center gap-3">
          <span className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${enabled ? "bg-green-500/20 text-green-400" : "bg-gray-500/20 text-gray-400"}`}>
            <span className={`h-2 w-2 rounded-full ${enabled ? "bg-green-400" : "bg-gray-500"}`} />
            {enabled ? "Scanner Active" : "Scanner Disabled"}
          </span>
        </div>
      </div>

      {/* Stats */}
      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard label="Total Scans" value={fmtNumber(stats?.total_scans)} />
        <StatCard label="Detections" value={fmtNumber(stats?.total_detections)} />
        <StatCard label="Rules Loaded" value={rules.length} />
        <StatCard label="Rules Enabled" value={rules.filter(r => r.enabled).length} />
      </div>

      {/* Rules Table */}
      <div className="mb-6 overflow-hidden rounded-lg border border-border bg-card">
        <div className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-white">Scanner Rules</h2>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-gray-400">
              <th className="px-4 py-2">Enabled</th>
              <th className="px-4 py-2">Name</th>
              <th className="px-4 py-2 hidden md:table-cell">Description</th>
              <th className="px-4 py-2">Action</th>
              <th className="px-4 py-2">Patterns</th>
              <th className="px-4 py-2">Detections</th>
            </tr>
          </thead>
          <tbody>
            {rules.map((rule) => (
              <tr key={rule.name} className="border-b border-border/50 hover:bg-gray-800/30">
                <td className="px-4 py-2">
                  <button
                    onClick={() => toggleRule(rule.name, !rule.enabled)}
                    disabled={updating === rule.name}
                    className={`h-5 w-9 rounded-full transition-colors ${rule.enabled ? "bg-green-500" : "bg-gray-600"} relative`}
                  >
                    <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${rule.enabled ? "left-[18px]" : "left-0.5"}`} />
                  </button>
                </td>
                <td className="px-4 py-2 font-mono text-xs text-white">{rule.name}</td>
                <td className="px-4 py-2 text-gray-400 hidden md:table-cell">{rule.description}</td>
                <td className="px-4 py-2">
                  <select
                    value={rule.action}
                    onChange={(e) => changeAction(rule.name, e.target.value)}
                    disabled={updating === rule.name}
                    className="rounded border border-border bg-gray-800 px-2 py-1 text-xs text-white"
                  >
                    {ACTION_OPTIONS.map((a) => (
                      <option key={a} value={a}>{a}</option>
                    ))}
                  </select>
                </td>
                <td className="px-4 py-2 text-gray-400">{rule.pattern_count}</td>
                <td className="px-4 py-2">
                  <span className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ${rule.detections > 0 ? "bg-red-500/20 text-red-400" : "text-gray-500"}`}>
                    {fmtNumber(rule.detections)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Model Skip Config */}
      <div className="mb-6 overflow-hidden rounded-lg border border-border bg-card">
        <div className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-white">Model Skip Configuration</h2>
          <p className="mt-1 text-xs text-gray-400">Models tagged as trusted skip DLP scanning (data stays local)</p>
        </div>
        <div className="p-4">
          <div className="grid gap-4 md:grid-cols-2">
            {/* Trusted Tags */}
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase text-gray-400">Trusted Tags</h3>
              <div className="flex flex-wrap gap-2">
                {(skipConfig.trusted_tags || []).map((tag) => (
                  <span key={tag} className="rounded-full bg-green-500/20 px-3 py-1 text-xs font-semibold text-green-400">
                    {tag}
                  </span>
                ))}
                {(skipConfig.trusted_tags || []).length === 0 && (
                  <span className="text-xs text-gray-500">None configured</span>
                )}
              </div>
            </div>

            {/* Skip Providers */}
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase text-gray-400">Skip Providers</h3>
              <div className="flex flex-wrap gap-2">
                {(skipConfig.skip_providers || []).map((p) => (
                  <span key={p} className="rounded-full bg-blue-500/20 px-3 py-1 text-xs font-semibold text-blue-400">
                    {p}
                  </span>
                ))}
                {(skipConfig.skip_providers || []).length === 0 && (
                  <span className="text-xs text-gray-500">None — all providers scanned</span>
                )}
              </div>
            </div>
          </div>

          {/* Model Tags */}
          {Object.keys(skipConfig.model_tags || {}).length > 0 && (
            <div className="mt-4">
              <h3 className="mb-2 text-xs font-semibold uppercase text-gray-400">Tagged Models</h3>
              <div className="grid gap-2 md:grid-cols-3 lg:grid-cols-4">
                {Object.entries(skipConfig.model_tags || {}).map(([prefix, tags]) => (
                  <div key={prefix} className="flex items-center gap-2 rounded bg-gray-800/50 px-3 py-2">
                    <span className="font-mono text-xs text-white">{prefix}:*</span>
                    {tags.map((tag) => (
                      <span key={tag} className="rounded bg-green-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-green-400">
                        {tag}
                      </span>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Settings */}
          <div className="mt-4 flex flex-wrap gap-4 border-t border-border/50 pt-4">
            <div className="text-xs">
              <span className="text-gray-400">Content Logging:</span>{" "}
              <span className="font-semibold text-white">{skipConfig.content_logging || "off"}</span>
            </div>
            <div className="text-xs">
              <span className="text-gray-400">Log Sanitization:</span>{" "}
              <span className={`font-semibold ${skipConfig.sanitize_logs ? "text-green-400" : "text-red-400"}`}>
                {skipConfig.sanitize_logs ? "enabled" : "disabled"}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
