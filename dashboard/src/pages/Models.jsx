import { Fragment, useEffect, useState, useCallback } from "react";
import { Header } from "./Overview.jsx";
import { api } from "../api.js";

const TIER_COLORS = {
  economy: "bg-green-500/20 text-green-400 border-green-500/30",
  standard: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  premium: "bg-purple-500/20 text-purple-400 border-purple-500/30",
};

const TIER_OPTIONS = ["economy", "standard", "premium"];

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

const EMPTY_FORM = {
  provider: "",
  id: "",
  display_name: "",
  tier: "economy",
  supports_tools: false,
  supports_json_mode: false,
  max_context_tokens: 8192,
  cost_per_1k_input: 0,
  cost_per_1k_output: 0,
};

export default function Models() {
  const [models, setModels] = useState([]);
  const [providerNames, setProviderNames] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState(null); // {provider, id} of model being edited
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [modelsRes, configRes] = await Promise.all([
        api.models(),
        api.config(),
      ]);
      setModels(modelsRes.data || []);
      setProviderNames((configRes.providers || []).map((p) => p.name));
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

  function flashSuccess(msg) {
    setSuccess(msg);
    setTimeout(() => setSuccess(null), 3000);
  }

  async function createModel(form) {
    setSaving(true);
    setError(null);
    try {
      const { provider, id, ...fields } = form;
      const res = await api.createModel(provider, id, fields);
      setModels(res.data || []);
      setAdding(false);
      flashSuccess(`Added model "${id}"`);
    } catch (e) {
      setError(e.message || "Failed to add model");
    } finally {
      setSaving(false);
    }
  }

  async function updateModel(provider, id, updates) {
    setSaving(true);
    setError(null);
    try {
      const res = await api.updateModel(provider, id, updates);
      setModels(res.data || []);
      setEditing(null);
      flashSuccess(`Updated model "${id}"`);
    } catch (e) {
      setError(e.message || "Failed to update model");
    } finally {
      setSaving(false);
    }
  }

  async function removeModel(provider, id) {
    setSaving(true);
    setError(null);
    try {
      const res = await api.deleteModel(provider, id);
      setModels(res.data || []);
      flashSuccess(`Removed model "${id}"`);
    } catch (e) {
      setError(e.message || "Failed to remove model");
    } finally {
      setSaving(false);
    }
  }

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
        <button
          onClick={() => {
            setEditing(null);
            setAdding((v) => !v);
          }}
          className="ml-2 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
        >
          {adding ? "Cancel" : "Add Model"}
        </button>
      </Header>

      {error && (
        <div className="mt-4 rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
          {error}
        </div>
      )}
      {success && (
        <div className="mt-4 rounded border border-green-500/30 bg-green-500/10 px-3 py-2 text-xs text-green-400">
          {success}
        </div>
      )}

      {adding && (
        <ModelForm
          title="Add Model"
          initial={EMPTY_FORM}
          providerOptions={providerNames.length ? providerNames : providers}
          lockProvider={false}
          lockId={false}
          saving={saving}
          onSubmit={createModel}
          onCancel={() => setAdding(false)}
        />
      )}

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
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 4 }).map((_, i) => (
                <tr key={i} className="border-b border-border/50">
                  {Array.from({ length: 9 }).map((_, j) => (
                    <td key={j} className="px-4 py-3">
                      <div className="skeleton h-5 w-20" />
                    </td>
                  ))}
                </tr>
              ))
            ) : filtered.length === 0 ? (
              <tr>
                <td
                  colSpan={9}
                  className="px-4 py-12 text-center text-gray-500"
                >
                  {models.length === 0
                    ? "No models configured"
                    : "No models match your filter"}
                </td>
              </tr>
            ) : (
              filtered.map((m) => {
                const isEditing =
                  editing &&
                  editing.provider === m.provider &&
                  editing.id === m.id;
                return (
                  <Fragment key={`${m.provider}:${m.id}`}>
                    <tr
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
                      <td className="px-4 py-3 text-right">
                        <div className="flex justify-end gap-3">
                          <button
                            onClick={() => {
                              setAdding(false);
                              setEditing(
                                isEditing
                                  ? null
                                  : { provider: m.provider, id: m.id }
                              );
                            }}
                            className="text-xs text-accent hover:opacity-80"
                          >
                            {isEditing ? "Close" : "Edit"}
                          </button>
                          <button
                            onClick={() => removeModel(m.provider, m.id)}
                            disabled={saving}
                            className="text-xs text-red-400/70 hover:text-red-400"
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                    {isEditing && (
                      <tr className="border-b border-border/50 bg-gray-800/20">
                        <td colSpan={9} className="px-4 py-4">
                          <ModelForm
                            title={`Edit ${m.id}`}
                            initial={{
                              provider: m.provider,
                              id: m.id,
                              display_name: m.display_name || "",
                              tier: m.tier,
                              supports_tools: m.supports_tools,
                              supports_json_mode: m.supports_json_mode,
                              max_context_tokens: m.max_context_tokens,
                              cost_per_1k_input: m.cost_per_1k_input,
                              cost_per_1k_output: m.cost_per_1k_output,
                            }}
                            providerOptions={[m.provider]}
                            lockProvider
                            lockId
                            saving={saving}
                            onSubmit={(form) => {
                              const { provider, id, ...updates } = form;
                              updateModel(provider, id, updates);
                            }}
                            onCancel={() => setEditing(null)}
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ModelForm({
  title,
  initial,
  providerOptions,
  lockProvider,
  lockId,
  saving,
  onSubmit,
  onCancel,
}) {
  const [form, setForm] = useState(initial);

  function set(key, value) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  return (
    <div className="mt-4 rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-semibold text-white">{title}</h3>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div>
          <label className="mb-1 block text-xs text-gray-400">Provider</label>
          {lockProvider ? (
            <div className="rounded border border-border bg-gray-800/50 px-2 py-1.5 text-sm text-gray-300">
              {form.provider}
            </div>
          ) : (
            <select
              value={form.provider}
              onChange={(e) => set("provider", e.target.value)}
              disabled={saving}
              className="w-full rounded border border-border bg-gray-800 px-2 py-1.5 text-sm text-white"
            >
              <option value="" disabled>
                Select provider
              </option>
              {providerOptions.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          )}
        </div>
        <div>
          <label className="mb-1 block text-xs text-gray-400">Model ID</label>
          {lockId ? (
            <div className="rounded border border-border bg-gray-800/50 px-2 py-1.5 font-mono text-sm text-gray-300">
              {form.id}
            </div>
          ) : (
            <input
              type="text"
              value={form.id}
              onChange={(e) => set("id", e.target.value)}
              disabled={saving}
              placeholder="claude-sonnet-5"
              className="w-full rounded border border-border bg-gray-800 px-2 py-1.5 font-mono text-sm text-white placeholder-gray-600"
            />
          )}
        </div>
        <div>
          <label className="mb-1 block text-xs text-gray-400">Display Name</label>
          <input
            type="text"
            value={form.display_name}
            onChange={(e) => set("display_name", e.target.value)}
            disabled={saving}
            className="w-full rounded border border-border bg-gray-800 px-2 py-1.5 text-sm text-white"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-gray-400">Tier</label>
          <select
            value={form.tier}
            onChange={(e) => set("tier", e.target.value)}
            disabled={saving}
            className="w-full rounded border border-border bg-gray-800 px-2 py-1.5 text-sm text-white"
          >
            {TIER_OPTIONS.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-gray-400">Max Context Tokens</label>
          <input
            type="number"
            min="0"
            value={form.max_context_tokens}
            onChange={(e) => set("max_context_tokens", Number(e.target.value))}
            disabled={saving}
            className="w-full rounded border border-border bg-gray-800 px-2 py-1.5 text-sm text-white"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-gray-400">Cost /1k Input</label>
          <input
            type="number"
            step="0.0001"
            min="0"
            value={form.cost_per_1k_input}
            onChange={(e) => set("cost_per_1k_input", Number(e.target.value))}
            disabled={saving}
            className="w-full rounded border border-border bg-gray-800 px-2 py-1.5 text-sm text-white"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-gray-400">Cost /1k Output</label>
          <input
            type="number"
            step="0.0001"
            min="0"
            value={form.cost_per_1k_output}
            onChange={(e) => set("cost_per_1k_output", Number(e.target.value))}
            disabled={saving}
            className="w-full rounded border border-border bg-gray-800 px-2 py-1.5 text-sm text-white"
          />
        </div>
        <div className="flex items-end gap-4">
          <label className="flex items-center gap-1.5 text-xs text-gray-300">
            <input
              type="checkbox"
              checked={form.supports_tools}
              onChange={(e) => set("supports_tools", e.target.checked)}
              disabled={saving}
            />
            Tools
          </label>
          <label className="flex items-center gap-1.5 text-xs text-gray-300">
            <input
              type="checkbox"
              checked={form.supports_json_mode}
              onChange={(e) => set("supports_json_mode", e.target.checked)}
              disabled={saving}
            />
            JSON mode
          </label>
        </div>
      </div>
      <div className="mt-4 flex gap-2 border-t border-border/50 pt-3">
        <button
          onClick={() => onSubmit(form)}
          disabled={saving || !form.provider || !form.id}
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          Save
        </button>
        <button
          onClick={onCancel}
          disabled={saving}
          className="rounded border border-border px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
