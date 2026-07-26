import { useEffect, useState, useCallback } from "react";
import { api, setDisplayTimezone, getGatewayKey, setGatewayKey } from "../api.js";

const TIER_OPTIONS = ["economy", "standard", "premium"];
const PROVIDER_OPTIONS = ["anthropic", "openai", "google", "ollama"];
const COMPRESSION_OPTIONS = ["", "low", "medium", "high"];
const COMPRESSION_TIER_OPTIONS = ["light", "medium", "heavy", "extreme"];
const VARIANT_STORE_OPTIONS = [
  { value: "", label: "Off" },
  { value: "age", label: "AGE (Postgres)" },
  { value: "neo4j", label: "Neo4j" },
];

const TIMEZONE_OPTIONS = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Phoenix",
  "America/Anchorage",
  "Pacific/Honolulu",
  "Europe/London",
  "Europe/Berlin",
  "Asia/Tokyo",
  "Australia/Sydney",
];

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

      {/* Server Settings */}
      <div className="mb-6 rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 text-sm font-semibold uppercase text-gray-400">Server</h2>
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm text-white">Display timezone</span>
              <p className="text-xs text-gray-500">
                All timestamps in the dashboard are displayed in this timezone.
                Data is always stored in UTC.
              </p>
            </div>
            <select
              value={config?.server?.display_timezone || "UTC"}
              onChange={async (e) => {
                const tz = e.target.value;
                setSaving(true);
                setError(null);
                try {
                  const updated = await api.updateServerConfig({ display_timezone: tz });
                  setConfig(updated);
                  setDisplayTimezone(tz);
                  flashSuccess(`Timezone set to ${tz}`);
                } catch (err) {
                  setError(err.message || "Failed to update timezone");
                } finally {
                  setSaving(false);
                }
              }}
              disabled={saving}
              className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
            >
              {TIMEZONE_OPTIONS.map((tz) => (
                <option key={tz} value={tz}>{tz.replace(/_/g, " ")}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm text-white">Log level</span>
              <p className="text-xs text-gray-500">
                Controls verbosity for the gateway and uvicorn access logs. Applies immediately.
              </p>
            </div>
            <select
              value={config?.server?.log_level || "info"}
              onChange={async (e) => {
                const log_level = e.target.value;
                setSaving(true);
                setError(null);
                try {
                  const updated = await api.updateServerConfig({ log_level });
                  setConfig(updated);
                  flashSuccess(`Log level set to ${log_level}`);
                } catch (err) {
                  setError(err.message || "Failed to update log level");
                } finally {
                  setSaving(false);
                }
              }}
              disabled={saving}
              className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
            >
              {["debug", "info", "warning", "error"].map((lvl) => (
                <option key={lvl} value={lvl}>{lvl}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm text-white">Log format</span>
              <p className="text-xs text-gray-500">Plain text or structured JSON lines.</p>
            </div>
            <select
              value={config?.server?.log_format || "plain"}
              onChange={async (e) => {
                const log_format = e.target.value;
                setSaving(true);
                setError(null);
                try {
                  const updated = await api.updateServerConfig({ log_format });
                  setConfig(updated);
                  flashSuccess(`Log format set to ${log_format}`);
                } catch (err) {
                  setError(err.message || "Failed to update log format");
                } finally {
                  setSaving(false);
                }
              }}
              disabled={saving}
              className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
            >
              {["plain", "json"].map((fmt) => (
                <option key={fmt} value={fmt}>{fmt}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm text-white">Log destination</span>
              <p className="text-xs text-gray-500">
                Write logs to stderr (container logs) or to a file ({config?.server?.log_file || "logs/loom.log"}).
              </p>
            </div>
            <select
              value={config?.server?.log_destination || "stderr"}
              onChange={async (e) => {
                const log_destination = e.target.value;
                setSaving(true);
                setError(null);
                try {
                  const updated = await api.updateServerConfig({ log_destination });
                  setConfig(updated);
                  flashSuccess(`Log destination set to ${log_destination}`);
                } catch (err) {
                  setError(err.message || "Failed to update log destination");
                } finally {
                  setSaving(false);
                }
              }}
              disabled={saving}
              className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
            >
              {["stderr", "file"].map((dest) => (
                <option key={dest} value={dest}>{dest}</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* API access / gateway keys */}
      <ApiAccessSettings
        saving={saving}
        setSaving={setSaving}
        setError={setError}
        flashSuccess={flashSuccess}
      />

      {/* Compression */}
      <CompressionSettings
        config={config}
        setConfig={setConfig}
        saving={saving}
        setSaving={setSaving}
        setError={setError}
        flashSuccess={flashSuccess}
      />

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

function ApiAccessSettings({ saving, setSaving, setError, flashSuccess }) {
  const [keys, setKeys] = useState(null);
  const [newKeyName, setNewKeyName] = useState("");
  const [createdKey, setCreatedKey] = useState(null);
  const [browserKey, setBrowserKey] = useState(getGatewayKey());

  const refreshKeys = useCallback(() => {
    api.gatewayKeys().then(setKeys).catch(() => setKeys(null));
  }, []);

  useEffect(() => {
    refreshKeys();
  }, [refreshKeys]);

  async function createKey() {
    const name = newKeyName.trim();
    if (!name) return;
    setSaving(true);
    setError(null);
    try {
      const created = await api.createGatewayKey(name);
      setCreatedKey(created);
      setNewKeyName("");
      // First key on a fresh install: adopt it for this browser immediately,
      // otherwise the very next dashboard request would 401.
      if (!getGatewayKey()) {
        setGatewayKey(created.key);
        setBrowserKey(created.key);
      }
      refreshKeys();
      flashSuccess(`Created key "${name}"`);
    } catch (e) {
      setError(e.message || "Failed to create key");
    } finally {
      setSaving(false);
    }
  }

  async function toggleKey(k) {
    setSaving(true);
    setError(null);
    try {
      await api.toggleGatewayKey(k.id, !k.enabled);
      refreshKeys();
    } catch (e) {
      setError(e.message || "Failed to update key");
    } finally {
      setSaving(false);
    }
  }

  async function deleteKey(k) {
    setSaving(true);
    setError(null);
    try {
      await api.deleteGatewayKey(k.id);
      refreshKeys();
      flashSuccess(`Deleted key "${k.name}"`);
    } catch (e) {
      setError(e.message || "Failed to delete key");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mb-6 rounded-lg border border-border bg-card p-5">
      <h2 className="mb-4 text-sm font-semibold uppercase text-gray-400">API Access</h2>
      <p className="mb-4 text-xs text-gray-500">
        Once at least one gateway key exists, every API and inference request must
        present one (header <span className="font-mono">x-loom-gateway-key</span>).
        With no keys, the gateway runs open — fine for first-run setup, not for
        anything reachable by others.
      </p>

      {/* Browser key */}
      <div className="mb-4 flex items-center justify-between">
        <div>
          <span className="text-sm text-white">This browser&apos;s key</span>
          <p className="text-xs text-gray-500">
            Used by the dashboard for its own API calls. Stored in this browser only.
          </p>
        </div>
        <input
          type="password"
          value={browserKey}
          onChange={(e) => {
            setBrowserKey(e.target.value);
            setGatewayKey(e.target.value.trim());
          }}
          placeholder="loom-..."
          className="w-64 rounded border border-border bg-gray-800 px-2 py-1 font-mono text-sm text-white placeholder-gray-600"
        />
      </div>

      {/* One-time display of a newly created key */}
      {createdKey && (
        <div className="mb-4 rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2">
          <p className="text-xs font-semibold text-amber-400">
            Key created — copy it now, it will not be shown again:
          </p>
          <code className="mt-1 block break-all font-mono text-xs text-amber-200">
            {createdKey.key}
          </code>
        </div>
      )}

      {/* Key list */}
      {keys === null ? (
        <p className="mb-3 text-xs text-gray-500">
          Key list unavailable (storage offline or this browser&apos;s key is missing/invalid).
        </p>
      ) : keys.length === 0 ? (
        <p className="mb-3 text-xs text-red-400">
          No gateway keys exist — authentication is disabled.
        </p>
      ) : (
        <div className="mb-3 space-y-2">
          {keys.map((k) => (
            <div
              key={k.id}
              className="flex items-center justify-between rounded border border-border/50 bg-gray-800/50 px-3 py-2"
            >
              <div>
                <span className="text-sm font-medium text-white">{k.name}</span>
                <span className="ml-2 font-mono text-xs text-gray-500">{k.key_preview}</span>
                {!k.enabled && (
                  <span className="ml-2 rounded bg-gray-700 px-1.5 py-0.5 text-[10px] text-gray-400">
                    disabled
                  </span>
                )}
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => toggleKey(k)}
                  disabled={saving}
                  className="text-xs text-gray-400 hover:text-white"
                >
                  {k.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  onClick={() => deleteKey(k)}
                  disabled={saving}
                  className="text-xs text-red-400/70 hover:text-red-400"
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create */}
      <div className="flex items-center gap-2 border-t border-border/50 pt-4">
        <input
          type="text"
          value={newKeyName}
          onChange={(e) => setNewKeyName(e.target.value)}
          placeholder="key name (e.g. laptop, ci)"
          className="flex-1 rounded border border-border bg-gray-800 px-3 py-1.5 text-sm text-white placeholder-gray-500"
          onKeyDown={(e) => e.key === "Enter" && createKey()}
        />
        <button
          onClick={createKey}
          disabled={saving || !newKeyName.trim()}
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          Create Key
        </button>
      </div>
    </div>
  );
}

function CompressionSettings({ config, setConfig, saving, setSaving, setError, flashSuccess }) {
  const comp = config?.compression || {};

  async function update(updates) {
    setSaving(true);
    setError(null);
    try {
      const updated = await api.updateCompression(updates);
      setConfig(updated);
      flashSuccess("Compression settings updated");
    } catch (e) {
      setError(e.message || "Failed to update compression settings");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mb-6 rounded-lg border border-border bg-card p-5">
      <h2 className="mb-4 text-sm font-semibold uppercase text-gray-400">Compression</h2>
      <p className="mb-4 text-xs text-gray-500">
        Conversation history compression reduces token usage for long-running sessions.
        Content is compressed proportionally to age — older messages are compressed harder.
      </p>
      <div className="space-y-3">
        {/* Enabled toggle */}
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm text-white">Enabled</span>
            <p className="text-xs text-gray-500">Master on/off switch for all compression</p>
          </div>
          <button
            onClick={() => update({ enabled: !comp.enabled })}
            disabled={saving}
            className={`h-5 w-9 rounded-full transition-colors ${comp.enabled ? "bg-green-500" : "bg-gray-600"} relative`}
          >
            <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${comp.enabled ? "left-[18px]" : "left-0.5"}`} />
          </button>
        </div>

        {/* Default tier */}
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm text-white">Default tier</span>
            <p className="text-xs text-gray-500">
              light (filler only) → medium (graduated) → heavy (aggressive) → extreme (fingerprints)
            </p>
          </div>
          <select
            value={comp.default_tier || "medium"}
            onChange={(e) => update({ default_tier: e.target.value })}
            disabled={saving}
            className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
          >
            {COMPRESSION_TIER_OPTIONS.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </div>

        {/* Tool results */}
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm text-white">Compress tool results</span>
            <p className="text-xs text-gray-500">
              Compress text inside tool_result blocks (typically 80-90% of agentic session tokens)
            </p>
          </div>
          <button
            onClick={() => update({ tool_results: !comp.tool_results })}
            disabled={saving}
            className={`h-5 w-9 rounded-full transition-colors ${comp.tool_results !== false ? "bg-green-500" : "bg-gray-600"} relative`}
          >
            <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${comp.tool_results !== false ? "left-[18px]" : "left-0.5"}`} />
          </button>
        </div>

        {/* Protect window */}
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm text-white">Protect window</span>
            <p className="text-xs text-gray-500">
              Last N messages are shielded from compression (prevents read loops)
            </p>
          </div>
          <input
            type="number"
            min="0"
            max="50"
            value={comp.tool_result_protect_window ?? 6}
            onChange={(e) => update({ tool_result_protect_window: parseInt(e.target.value) || 0 })}
            disabled={saving}
            className="w-20 rounded border border-border bg-gray-800 px-2 py-1 text-right text-sm text-white"
          />
        </div>

        {/* Loop multiplier */}
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm text-white">Loop multiplier</span>
            <p className="text-xs text-gray-500">
              When a compression loop is detected, protect window is widened by this factor
            </p>
          </div>
          <input
            type="number"
            min="1"
            max="10"
            value={comp.loop_detected_protect_multiplier ?? 3}
            onChange={(e) => update({ loop_detected_protect_multiplier: parseInt(e.target.value) || 1 })}
            disabled={saving}
            className="w-20 rounded border border-border bg-gray-800 px-2 py-1 text-right text-sm text-white"
          />
        </div>

        {/* Variant store */}
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm text-white">Variant store</span>
            <p className="text-xs text-gray-500">
              Preserves originals for pointer resolution and enables relevance-aware compression
            </p>
          </div>
          <select
            value={comp.variant_store || ""}
            onChange={(e) => update({ variant_store: e.target.value })}
            disabled={saving}
            className="rounded border border-border bg-gray-800 px-2 py-1 text-sm text-white"
          >
            {VARIANT_STORE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>

        {/* LLM Prose section */}
        <div className="border-t border-border/50 pt-3 mt-3">
          <h3 className="mb-3 text-xs font-semibold uppercase text-gray-500">LLM Prose Compression</h3>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <span className="text-sm text-white">LLM prose</span>
                <p className="text-xs text-gray-500">
                  Use a local model for prose summarization instead of extractive compression
                </p>
              </div>
              <button
                onClick={() => update({ llm_prose: !comp.llm_prose })}
                disabled={saving}
                className={`h-5 w-9 rounded-full transition-colors ${comp.llm_prose ? "bg-green-500" : "bg-gray-600"} relative`}
              >
                <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${comp.llm_prose ? "left-[18px]" : "left-0.5"}`} />
              </button>
            </div>

            {comp.llm_prose && (
              <>
                <div className="flex items-center justify-between">
                  <div>
                    <span className="text-sm text-white">Allow LAN endpoints</span>
                    <p className="text-xs text-gray-500">
                      Permit private-network LLM URLs (e.g. an Ollama box on 192.168.x).
                      Localhost is always allowed.
                    </p>
                  </div>
                  <button
                    onClick={() => update({ allow_private_llm_url: !comp.allow_private_llm_url })}
                    disabled={saving}
                    className={`h-5 w-9 rounded-full transition-colors ${comp.allow_private_llm_url ? "bg-green-500" : "bg-gray-600"} relative`}
                  >
                    <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${comp.allow_private_llm_url ? "left-[18px]" : "left-0.5"}`} />
                  </button>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-white">LLM URL</span>
                  <input
                    type="text"
                    value={comp.llm_url || ""}
                    onChange={(e) => update({ llm_url: e.target.value })}
                    disabled={saving}
                    placeholder="http://localhost:11434"
                    className="w-64 rounded border border-border bg-gray-800 px-2 py-1 font-mono text-sm text-white placeholder-gray-600"
                  />
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-white">LLM model</span>
                  <input
                    type="text"
                    value={comp.llm_model || ""}
                    onChange={(e) => update({ llm_model: e.target.value })}
                    disabled={saving}
                    placeholder="qwen2.5:7b"
                    className="w-48 rounded border border-border bg-gray-800 px-2 py-1 font-mono text-sm text-white placeholder-gray-600"
                  />
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-white">Timeout (seconds)</span>
                  <input
                    type="number"
                    min="1"
                    max="120"
                    value={comp.llm_timeout_seconds ?? 30}
                    onChange={(e) => update({ llm_timeout_seconds: parseFloat(e.target.value) || 30 })}
                    disabled={saving}
                    className="w-20 rounded border border-border bg-gray-800 px-2 py-1 text-right text-sm text-white"
                  />
                </div>
              </>
            )}
          </div>
        </div>
      </div>
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
