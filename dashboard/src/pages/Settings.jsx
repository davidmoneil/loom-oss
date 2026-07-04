import { useEffect, useState, useCallback } from "react";
import { api } from "../api.js";

const TIER_OPTIONS = ["economy", "standard", "premium"];
const PROVIDER_OPTIONS = ["anthropic", "openai", "google", "ollama"];
const COMPRESSION_OPTIONS = ["", "low", "medium", "high"];

export default function Settings() {
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [newSourceName, setNewSourceName] = useState("");

  const refresh = useCallback(() => {
    api.config()
      .then(setConfig)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function flashSuccess(msg) {
    setSuccess(msg);
    setTimeout(() => setSuccess(null), 3000);
  }

  async function updateSource(name, updates) {
    setSaving(true);
    setError(null);
    try {
      const updated = await api.updateSourcePolicy(name, updates);
      setConfig(updated);
      flashSuccess(`Updated source "${name}"`);
    } catch (e) {
      setError(e.message || "Failed to update source policy");
    } finally {
      setSaving(false);
    }
  }

  async function addSource() {
    const name = newSourceName.trim();
    if (!name) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.createSourcePolicy(name, {});
      setConfig(updated);
      setNewSourceName("");
      flashSuccess(`Created source "${name}"`);
    } catch (e) {
      setError(e.message || "Failed to create source");
    } finally {
      setSaving(false);
    }
  }

  async function removeSource(name) {
    setSaving(true);
    setError(null);
    try {
      const updated = await api.deleteSourcePolicy(name);
      setConfig(updated);
      flashSuccess(`Removed source "${name}"`);
    } catch (e) {
      setError(e.message || "Failed to remove source");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="p-6">
        <h1 className="mb-6 text-2xl font-bold text-white">Settings</h1>
        <div className="animate-pulse text-gray-400">Loading configuration...</div>
      </div>
    );
  }

  const sources = config?.sources || {};
  const providers = config?.providers || [];
  const routing = config?.routing || {};

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-white">Settings</h1>
        <p className="mt-1 text-sm text-gray-400">
          Gateway configuration and source routing policies.
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
          {error}
        </div>
      )}
      {success && (
        <div className="mb-4 rounded border border-green-500/30 bg-green-500/10 px-3 py-2 text-xs text-green-400">
          {success}
        </div>
      )}

      {/* Overview cards */}
      <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-semibold uppercase text-gray-400">Providers</div>
          <div className="mt-1 text-lg font-bold text-white">{providers.length}</div>
          <div className="mt-1 text-xs text-gray-500">
            {providers.map((p) => p.name).join(", ") || "None configured"}
          </div>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-semibold uppercase text-gray-400">Source Policies</div>
          <div className="mt-1 text-lg font-bold text-white">{Object.keys(sources).length}</div>
          <div className="mt-1 text-xs text-gray-500">
            {Object.values(sources).filter((s) => s.per_turn_routing).length} with per-turn routing
          </div>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-semibold uppercase text-gray-400">Routing</div>
          <div className="mt-1 text-lg font-bold text-white">
            {routing.default_determinism_target != null
              ? `${(routing.default_determinism_target * 100).toFixed(0)}%`
              : "—"}
          </div>
          <div className="mt-1 text-xs text-gray-500">
            Determinism target &middot; Min {routing.min_empirical_runs ?? "—"} runs
          </div>
        </div>
      </div>

      {/* Source Policies */}
      <div className="rounded-lg border border-border bg-card p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase text-gray-400">Source Policies</h2>
        </div>
        <p className="mb-4 text-xs text-gray-500">
          Each source represents a client identity. Policies control which models, providers,
          and routing modes are available per source.
        </p>

        {Object.entries(sources).length === 0 && (
          <p className="mb-4 text-sm text-gray-400">No source policies configured.</p>
        )}

        <div className="space-y-4">
          {Object.entries(sources).map(([name, policy]) => (
            <SourceCard
              key={name}
              name={name}
              policy={policy}
              saving={saving}
              onUpdate={(updates) => updateSource(name, updates)}
              onRemove={() => removeSource(name)}
            />
          ))}
        </div>

        <div className="mt-4 flex items-center gap-2 border-t border-border/50 pt-4">
          <input
            type="text"
            value={newSourceName}
            onChange={(e) => setNewSourceName(e.target.value)}
            placeholder="new-source-name"
            className="flex-1 rounded border border-border bg-gray-800 px-3 py-1.5 font-mono text-sm text-white placeholder-gray-500"
            onKeyDown={(e) => e.key === "Enter" && addSource()}
          />
          <button
            onClick={addSource}
            disabled={saving || !newSourceName.trim()}
            className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            Add Source
          </button>
        </div>
      </div>

      {/* Providers (read-only) */}
      {providers.length > 0 && (
        <div className="mt-6 rounded-lg border border-border bg-card p-5">
          <h2 className="mb-3 text-sm font-semibold uppercase text-gray-400">Providers</h2>
          <p className="mb-3 text-xs text-gray-500">
            Configured via loom.yaml. Shown here for reference.
          </p>
          <div className="space-y-2">
            {providers.map((p) => (
              <div key={p.name} className="flex items-center justify-between rounded border border-border/50 bg-gray-800/50 px-3 py-2">
                <div>
                  <span className="text-sm font-medium text-white">{p.name}</span>
                  <span className="ml-2 text-xs text-gray-500">{p.api_base}</span>
                </div>
                <span className="text-xs text-gray-400">
                  {p.models?.length || 0} model{(p.models?.length || 0) !== 1 ? "s" : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SourceCard({ name, policy, saving, onUpdate, onRemove }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded border border-border/50 bg-gray-800/30">
      <div
        className="flex cursor-pointer items-center justify-between px-4 py-3"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm font-medium text-white">{name}</span>
          <div className="flex gap-1.5">
            {policy.per_turn_routing && (
              <span className="rounded bg-blue-500/20 px-1.5 py-0.5 text-[10px] font-medium text-blue-400 border border-blue-500/30">
                per-turn
              </span>
            )}
            {policy.pinned_model && (
              <span className="rounded bg-purple-500/20 px-1.5 py-0.5 text-[10px] font-medium text-purple-400 border border-purple-500/30">
                pinned
              </span>
            )}
            <span className="rounded bg-gray-700/50 px-1.5 py-0.5 text-[10px] font-medium text-gray-400 border border-border">
              {policy.minimum_tier}
            </span>
          </div>
        </div>
        <svg
          className={`h-4 w-4 text-gray-400 transition-transform ${expanded ? "rotate-180" : ""}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2"
        >
          <path d="M19 9l-7 7-7-7" />
        </svg>
      </div>

      {expanded && (
        <div className="border-t border-border/50 px-4 py-3 space-y-3">
          {/* Per-Turn Routing toggle */}
          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm text-white">Per-turn routing</span>
              <p className="text-xs text-gray-500">
                Route individual turns to different models based on complexity
              </p>
            </div>
            <button
              onClick={() => onUpdate({ per_turn_routing: !policy.per_turn_routing })}
              disabled={saving}
              className={`h-5 w-9 rounded-full transition-colors ${policy.per_turn_routing ? "bg-blue-500" : "bg-gray-600"} relative`}
            >
              <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${policy.per_turn_routing ? "left-[18px]" : "left-0.5"}`} />
            </button>
          </div>

          {/* Minimum Tier */}
          <div className="flex items-center justify-between">
            <span className="text-sm text-white">Minimum tier</span>
            <select
              value={policy.minimum_tier}
              onChange={(e) => onUpdate({ minimum_tier: e.target.value })}
              disabled={saving}
              className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
            >
              {TIER_OPTIONS.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>

          {/* Pinned Model */}
          <div className="flex items-center justify-between">
            <span className="text-sm text-white">Pinned model</span>
            <input
              type="text"
              value={policy.pinned_model || ""}
              onChange={(e) => onUpdate({ pinned_model: e.target.value || null })}
              disabled={saving}
              placeholder="none"
              className="w-48 rounded border border-border bg-gray-800 px-2 py-1 text-right font-mono text-sm text-white placeholder-gray-600"
            />
          </div>

          {/* Compression Tier */}
          <div className="flex items-center justify-between">
            <span className="text-sm text-white">Compression tier</span>
            <select
              value={policy.compression_tier || ""}
              onChange={(e) => onUpdate({ compression_tier: e.target.value || null })}
              disabled={saving}
              className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
            >
              {COMPRESSION_OPTIONS.map((t) => (
                <option key={t} value={t}>{t || "default"}</option>
              ))}
            </select>
          </div>

          {/* Requires Tools */}
          <div className="flex items-center justify-between">
            <span className="text-sm text-white">Requires tools</span>
            <button
              onClick={() => onUpdate({ requires_tools: !policy.requires_tools })}
              disabled={saving}
              className={`h-5 w-9 rounded-full transition-colors ${policy.requires_tools ? "bg-green-500" : "bg-gray-600"} relative`}
            >
              <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${policy.requires_tools ? "left-[18px]" : "left-0.5"}`} />
            </button>
          </div>

          {/* Allowed Providers */}
          <div>
            <span className="text-sm text-white">Allowed providers</span>
            <div className="mt-1.5 flex flex-wrap gap-2">
              {PROVIDER_OPTIONS.map((p) => {
                const active = (policy.allowed_providers || []).includes(p);
                return (
                  <button
                    key={p}
                    onClick={() => {
                      const current = policy.allowed_providers || [];
                      const next = active
                        ? current.filter((x) => x !== p)
                        : [...current, p];
                      onUpdate({ allowed_providers: next });
                    }}
                    disabled={saving}
                    className={`rounded border px-2 py-1 text-xs font-medium transition-colors ${
                      active
                        ? "border-accent/50 bg-accent/20 text-accent"
                        : "border-border bg-gray-800 text-gray-500 hover:text-gray-300"
                    }`}
                  >
                    {p}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Remove */}
          <div className="border-t border-border/50 pt-3">
            <button
              onClick={onRemove}
              disabled={saving}
              className="text-xs text-red-400/70 hover:text-red-400"
            >
              Remove source policy
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
