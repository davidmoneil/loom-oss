import { useEffect, useState, useCallback } from "react";
import { api } from "../api.js";

const TIERS = ["moderate", "elevated", "high", "critical"];
const CLASS_OPTIONS = ["critical", "high", "standard", "low"];

const TIER_COLORS = {
  normal: "bg-gray-500/20 text-gray-400 border-gray-500/30",
  moderate: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  elevated: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  high: "bg-red-500/20 text-red-400 border-red-500/30",
  critical: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  disabled: "bg-gray-500/20 text-gray-400 border-gray-500/30",
};

const CLASS_COLORS = {
  critical: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  high: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  low: "bg-gray-700/50 text-gray-400 border-border",
  standard: "bg-gray-700/50 text-gray-300 border-border",
};

export default function Governor() {
  const [settings, setSettings] = useState(null);
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [newOverrideJob, setNewOverrideJob] = useState("");
  const [newOverrideClass, setNewOverrideClass] = useState("standard");

  const refresh = useCallback(() => {
    Promise.all([api.governorSettings(), api.governorStatus()])
      .then(([settingsData, statusData]) => {
        setSettings(settingsData);
        setStatus(statusData);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
  }, [refresh]);

  async function applyUpdate(updates) {
    setSaving(true);
    setError(null);
    try {
      const updated = await api.updateGovernor(updates);
      setSettings(updated);
      refresh();
    } catch (e) {
      setError(e.message || "Failed to update governor settings");
    } finally {
      setSaving(false);
    }
  }

  async function removeOverride(job) {
    setSaving(true);
    try {
      const updated = await api.deleteGovernorOverride(job);
      setSettings(updated);
    } catch (e) {
      setError(e.message || "Failed to remove override");
    } finally {
      setSaving(false);
    }
  }

  function addOverride() {
    const job = newOverrideJob.trim();
    if (!job) return;
    applyUpdate({ class_overrides: { [job]: newOverrideClass } });
    setNewOverrideJob("");
  }

  if (loading) {
    return (
      <div className="p-6">
        <h1 className="mb-6 text-2xl font-bold text-white">Throttle Governor</h1>
        <div className="animate-pulse text-gray-400">Loading governor settings...</div>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="p-6">
        <h1 className="mb-6 text-2xl font-bold text-white">Throttle Governor</h1>
        <div className="text-gray-400">Governor is not available on this gateway.</div>
      </div>
    );
  }

  const enabled = settings.enabled !== false;
  const tier = status?.tier ?? "unknown";
  const thresholds = settings.tier_thresholds || {};
  const overrides = settings.class_overrides || {};

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Throttle Governor</h1>
        <span className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium ${TIER_COLORS[tier] ?? TIER_COLORS.normal}`}>
          <span className={`h-2 w-2 rounded-full ${enabled ? "bg-green-400" : "bg-gray-500"}`} />
          {tier}
        </span>
      </div>

      {error && (
        <div className="mb-4 rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
          {error}
        </div>
      )}

      <div className="mb-6 rounded-lg border border-border bg-card p-5">
        <p className="mb-4 text-xs text-gray-400">
          Graduated utilization-aware throttling for gateway-side automation. Settings here are
          shared with the Nexus dashboard's governor for feature parity.
        </p>

        <label className="flex items-center gap-3">
          <button
            onClick={() => applyUpdate({ enabled: !enabled })}
            disabled={saving}
            className={`h-5 w-9 rounded-full transition-colors ${enabled ? "bg-green-500" : "bg-gray-600"} relative`}
          >
            <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${enabled ? "left-[18px]" : "left-0.5"}`} />
          </button>
          <span className="text-sm text-white">Enable throttle governor</span>
        </label>

        {status && (
          <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-gray-400">
            <span>Util 5h: <strong className="text-white">{((status.util_5h || 0) * 100).toFixed(0)}%</strong></span>
            <span>Util 7d: <strong className="text-white">{((status.util_7d || 0) * 100).toFixed(0)}%</strong></span>
            <span>{status.jobs_throttled || 0} throttled</span>
            <span>{status.jobs_skipped || 0} skipped</span>
          </div>
        )}

        {/* Tier Thresholds */}
        <div className="mt-5 border-t border-border/50 pt-4">
          <h3 className="mb-2 text-xs font-semibold uppercase text-gray-400">Tier Thresholds</h3>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {TIERS.map((t) => (
              <div key={t} className="space-y-1">
                <label className="text-xs capitalize text-gray-400">{t}</label>
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={thresholds[t] ?? ""}
                  disabled={saving}
                  onChange={(e) => {
                    const val = parseInt(e.target.value, 10);
                    if (Number.isNaN(val)) return;
                    applyUpdate({ tier_thresholds: { [t]: val } });
                  }}
                  className="w-full rounded border border-border bg-gray-800 px-2 py-1 text-center text-sm text-white"
                />
              </div>
            ))}
          </div>
          <p className="mt-1 text-xs text-gray-500">Utilization % at which each tier activates.</p>
        </div>

        {/* Class Overrides */}
        <div className="mt-5 border-t border-border/50 pt-4">
          <h3 className="mb-2 text-xs font-semibold uppercase text-gray-400">Class Overrides</h3>
          <p className="mb-3 text-xs text-gray-400">
            Temporarily override a job's throttle class without editing YAML.
          </p>

          {Object.entries(overrides).length > 0 && (
            <div className="mb-3 space-y-1">
              {Object.entries(overrides).map(([job, cls]) => (
                <div key={job} className="flex items-center gap-2">
                  <span className="flex-1 font-mono text-sm text-white">{job}</span>
                  <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${CLASS_COLORS[cls] ?? CLASS_COLORS.standard}`}>
                    {cls}
                  </span>
                  <button
                    onClick={() => removeOverride(job)}
                    disabled={saving}
                    className="text-xs text-red-400/70 hover:text-red-400"
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="flex items-center gap-2">
            <input
              type="text"
              value={newOverrideJob}
              onChange={(e) => setNewOverrideJob(e.target.value)}
              placeholder="job-name"
              className="flex-1 rounded border border-border bg-gray-800 px-3 py-1.5 font-mono text-sm text-white placeholder-gray-500"
              onKeyDown={(e) => e.key === "Enter" && addOverride()}
            />
            <select
              value={newOverrideClass}
              onChange={(e) => setNewOverrideClass(e.target.value)}
              className="rounded border border-border bg-gray-800 px-2 py-1.5 text-sm text-white"
            >
              {CLASS_OPTIONS.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
            <button
              onClick={addOverride}
              disabled={saving}
              className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
            >
              Add
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
