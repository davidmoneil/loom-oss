import { useEffect, useState, useCallback } from "react";
import { Header } from "./Overview.jsx";
import {
  api,
  fmtNumber,
  fmtCost,
  fmtLatency,
  fmtTime,
} from "../api.js";

const PAGE_SIZE = 50;

export default function Audit() {
  const [entries, setEntries] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [models, setModels] = useState([]);

  // Filter inputs (search is debounced into `filters`).
  const [searchInput, setSearchInput] = useState("");
  const [skillInput, setSkillInput] = useState("");
  const [filters, setFilters] = useState({
    search: "",
    model: "",
    source: "",
    status: "",
    skill: "",
  });

  // Expand state
  const [expandedRows, setExpandedRows] = useState(new Set());
  const [contentCache, setContentCache] = useState({});
  const [contentLoading, setContentLoading] = useState({});
  const [contentError, setContentError] = useState({});

  useEffect(() => {
    api
      .models()
      .then((d) => setModels((d?.data || []).map((m) => m.id)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const id = setTimeout(
      () => setFilters((f) => ({ ...f, search: searchInput })),
      300
    );
    return () => clearTimeout(id);
  }, [searchInput]);

  useEffect(() => {
    const id = setTimeout(
      () => setFilters((f) => ({ ...f, skill: skillInput })),
      300
    );
    return () => clearTimeout(id);
  }, [skillInput]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.audit({
        limit: PAGE_SIZE,
        offset,
        model: filters.model,
        source: filters.source,
        status: filters.status,
        search: filters.search,
        skill: filters.skill,
      });
      setEntries(data.entries || []);
      setTotal(data.total || 0);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [offset, filters]);

  useEffect(() => {
    load();
  }, [load]);

  // Reset to first page whenever filters change.
  useEffect(() => {
    setOffset(0);
  }, [filters]);

  const setFilter = (k, v) => setFilters((f) => ({ ...f, [k]: v }));

  const toggleExpanded = useCallback(
    async (requestId) => {
      setExpandedRows((prev) => {
        const next = new Set(prev);
        if (next.has(requestId)) {
          next.delete(requestId);
        } else {
          next.add(requestId);
          if (!contentCache[requestId]) {
            setContentLoading((prev) => ({ ...prev, [requestId]: true }));
            api
              .auditContent(requestId)
              .then((data) => {
                setContentCache((prev) => ({ ...prev, [requestId]: data }));
                setContentError((prev) => ({ ...prev, [requestId]: null }));
              })
              .catch((e) => {
                // 404 = no content row for this request (content_logging off,
                // or the request predates content capture) — render the
                // "No content logged" empty state, not a fetch error.
                const notFound = e.message.endsWith("-> 404");
                setContentError((prev) => ({
                  ...prev,
                  [requestId]: notFound ? null : e.message,
                }));
              })
              .finally(() => {
                setContentLoading((prev) => ({ ...prev, [requestId]: false }));
              });
          }
        }
        return next;
      });
    },
    [contentCache]
  );

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="p-6">
      <Header title="Audit" error={error} onRefresh={load} />

      <div className="mt-6 flex flex-wrap gap-2">
        <input
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder="Search request id, model, reason…"
          className="min-w-[220px] flex-1 rounded-md border border-border bg-card px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-accent focus:outline-none"
        />
        <Select
          value={filters.model}
          onChange={(v) => setFilter("model", v)}
          placeholder="All models"
          options={models}
        />
        <Select
          value={filters.source}
          onChange={(v) => setFilter("source", v)}
          placeholder="All sources"
          options={["default", "critical"]}
        />
        <Select
          value={filters.status}
          onChange={(v) => setFilter("status", v)}
          placeholder="All statuses"
          options={["success", "error"]}
        />
        <input
          value={skillInput}
          onChange={(e) => setSkillInput(e.target.value)}
          placeholder="Skill (e.g. end-session)"
          className="w-[180px] rounded-md border border-border bg-card px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-accent focus:outline-none"
        />
      </div>

      <div className="mt-4 overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[1000px] text-left text-sm">
          <thead className="bg-card text-xs uppercase tracking-wide text-gray-400">
            <tr>
              <Th>Time</Th>
              <Th>Source</Th>
              <Th>Session</Th>
              <Th>Requested</Th>
              <Th>Routed To</Th>
              <Th>Task</Th>
              <Th>Skill</Th>
              <Th className="text-right">Tokens (in/out)</Th>
              <Th className="text-right">Request Time</Th>
              <Th className="text-right">Cost</Th>
              <Th>Reason</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {loading ? (
              <SkeletonRows />
            ) : entries.length === 0 ? (
              <tr>
                <td colSpan={12} className="p-8 text-center text-gray-500">
                  No requests match the current filters
                </td>
              </tr>
            ) : (
              entries.flatMap((e) => [
                <tr key={e.request_id} className="bg-base hover:bg-gray-800/40">
                  <Td className="whitespace-nowrap text-gray-400">
                    <button
                      onClick={() => toggleExpanded(e.request_id)}
                      className="inline-flex items-center gap-2 hover:opacity-70"
                    >
                      <svg
                        className={`h-4 w-4 transition-transform ${expandedRows.has(e.request_id) ? "rotate-180" : ""}`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth="2"
                      >
                        <path d="M19 9l-7 7-7-7" />
                      </svg>
                      {fmtTime(e.timestamp)}
                    </button>
                  </Td>
                  <Td>{e.source}</Td>
                  <Td className="text-gray-300 font-mono text-xs">
                    {e.session_id ? e.session_id.slice(0, 12) : "—"}
                  </Td>
                  <Td className="text-gray-400">{e.requested_model}</Td>
                  <Td className="font-medium text-gray-100">{e.model_used}</Td>
                  <Td>{e.task_type}</Td>
                  <Td className="text-gray-300">{e.skill || "—"}</Td>
                  <Td className="text-right tabular-nums text-gray-300">
                    <div>{fmtNumber(e.tokens_in)} / {fmtNumber(e.tokens_out)}</div>
                    {(e.cache_read_tokens > 0 || e.cache_creation_tokens > 0) && (
                      <div className="text-[10px] text-teal-400">
                        {e.cache_read_tokens > 0 && <>{fmtNumber(e.cache_read_tokens)} cached</>}
                        {e.cache_read_tokens > 0 && e.cache_creation_tokens > 0 && " / "}
                        {e.cache_creation_tokens > 0 && <span className="text-amber-400">{fmtNumber(e.cache_creation_tokens)} written</span>}
                      </div>
                    )}
                  </Td>
                  <Td className="text-right tabular-nums">
                    {fmtLatency(e.latency_ms)}
                  </Td>
                  <Td className="text-right tabular-nums">
                    {fmtCost(e.cost_estimate)}
                  </Td>
                  <Td className="text-gray-400">{e.routing_reason || "—"}</Td>
                  <Td>
                    <StatusBadge status={e.status} />
                  </Td>
                </tr>,
                expandedRows.has(e.request_id) && (
                  <tr key={`${e.request_id}-content`} className="bg-gray-900/20">
                    <td colSpan={12} className="px-3 py-3">
                      <ExpandedContent
                        content={contentCache[e.request_id]}
                        isLoading={contentLoading[e.request_id]}
                        error={contentError[e.request_id]}
                      />
                    </td>
                  </tr>
                ),
              ])
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-4 flex items-center justify-between text-sm text-gray-400">
        <span>
          {total === 0
            ? "0 entries"
            : `Showing ${offset + 1}–${Math.min(
                offset + PAGE_SIZE,
                total
              )} of ${fmtNumber(total)}`}
        </span>
        <div className="flex items-center gap-2">
          <PageBtn
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Prev
          </PageBtn>
          <span className="text-gray-500">
            Page {page} / {pages}
          </span>
          <PageBtn
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </PageBtn>
        </div>
      </div>
    </div>
  );
}

function Select({ value, onChange, placeholder, options }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md border border-border bg-card px-3 py-2 text-sm text-gray-200 focus:border-accent focus:outline-none"
    >
      <option value="">{placeholder}</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function StatusBadge({ status }) {
  const ok = status === "success";
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-xs font-medium ${
        ok
          ? "bg-green-500/15 text-green-400"
          : "bg-red-500/15 text-red-400"
      }`}
    >
      {status}
    </span>
  );
}

function Th({ children, className = "" }) {
  return <th className={`px-3 py-2 font-medium ${className}`}>{children}</th>;
}
function Td({ children, className = "" }) {
  return <td className={`px-3 py-2 ${className}`}>{children}</td>;
}
function PageBtn({ children, disabled, onClick }) {
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className="rounded-md border border-border bg-card px-3 py-1.5 text-gray-300 hover:bg-gray-700/50 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}
function SkeletonRows() {
  return Array.from({ length: 8 }).map((_, i) => (
    <tr key={i} className="bg-base">
      {Array.from({ length: 12 }).map((__, j) => (
        <td key={j} className="px-3 py-3">
          <div className="skeleton h-4 w-full" />
        </td>
      ))}
    </tr>
  ));
}

const ROLE_STYLES = {
  user: "bg-blue-500/25 text-blue-300 border-blue-500/40",
  assistant: "bg-emerald-500/25 text-emerald-300 border-emerald-500/40",
  system: "bg-amber-500/25 text-amber-300 border-amber-500/40",
};

const BLOCK_STYLES = {
  text: { border: "border-emerald-500/30", bg: "bg-emerald-900/15", label: "text", color: "text-emerald-400" },
  thinking: { border: "border-purple-500/30", bg: "bg-purple-900/15", label: "thinking", color: "text-purple-400" },
  tool_use: { border: "border-cyan-500/30", bg: "bg-cyan-900/15", label: "tool_use", color: "text-cyan-400" },
  tool_result: { border: "border-orange-500/30", bg: "bg-orange-900/15", label: "tool_result", color: "text-orange-400" },
};

function ContentBlockTag({ type }) {
  const style = BLOCK_STYLES[type] || { color: "text-gray-400", label: type };
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${style.color} bg-gray-700/40`}>
      {style.label}
    </span>
  );
}

function MetadataTag({ label, value, color = "text-gray-400" }) {
  if (!value) return null;
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-gray-700/50 ${color}`}>
      <span className="opacity-60">{label}:</span> {value}
    </span>
  );
}

function UsageBar({ usage }) {
  if (!usage) return null;
  const input = usage.input_tokens || 0;
  const output = usage.output_tokens || 0;
  const cacheRead = usage.cache_read_input_tokens || 0;
  const cacheCreation = usage.cache_creation_input_tokens || 0;
  const total = input + output + cacheRead + cacheCreation;
  if (!total) return null;

  const segments = [];
  if (cacheRead > 0) segments.push({ tokens: cacheRead, label: "cache read", cls: "bg-teal-500" });
  if (cacheCreation > 0) segments.push({ tokens: cacheCreation, label: "cache write", cls: "bg-amber-500" });
  if (input > 0) segments.push({ tokens: input, label: "input", cls: "bg-blue-500" });
  if (output > 0) segments.push({ tokens: output, label: "output", cls: "bg-emerald-500" });

  return (
    <div className="rounded-md bg-gray-800/60 border border-border/40 p-2.5">
      <div className="mb-1.5 flex items-center gap-2 text-[10px] font-medium uppercase tracking-wider text-gray-400">
        Token Usage
        <span className="font-mono text-gray-300">{total.toLocaleString()}</span>
      </div>
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-gray-700/50">
        {segments.map((s, i) => (
          <div
            key={i}
            className={`${s.cls} transition-all`}
            style={{ width: `${(s.tokens / total) * 100}%` }}
            title={`${s.label}: ${s.tokens.toLocaleString()}`}
          />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px]">
        {segments.map((s, i) => (
          <span key={i} className="flex items-center gap-1 text-gray-300">
            <span className={`inline-block h-2 w-2 rounded-full ${s.cls}`} />
            {s.label}: <span className="font-mono">{s.tokens.toLocaleString()}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function renderContentBlocks(content) {
  if (typeof content === "string") {
    return (
      <pre className="whitespace-pre-wrap break-words text-xs text-gray-100 font-mono overflow-x-auto">
        {content}
      </pre>
    );
  }
  if (!Array.isArray(content)) {
    return (
      <pre className="whitespace-pre-wrap break-words text-xs text-gray-100 font-mono overflow-x-auto">
        {JSON.stringify(content, null, 2)}
      </pre>
    );
  }
  return (
    <div className="space-y-1.5">
      {content.map((block, i) => {
        if (!block || typeof block !== "object") {
          return (
            <pre key={i} className="whitespace-pre-wrap break-words text-xs text-gray-100 font-mono overflow-x-auto">
              {JSON.stringify(block, null, 2)}
            </pre>
          );
        }
        const blockType = block.type || "text";
        const style = BLOCK_STYLES[blockType] || BLOCK_STYLES.text;
        let blockContent;
        if (blockType === "text") {
          blockContent = block.text || "";
        } else if (blockType === "thinking") {
          blockContent = block.thinking || "";
        } else if (blockType === "tool_use") {
          blockContent = `${block.name || "unknown"}(${JSON.stringify(block.input || {}, null, 2)})`;
        } else if (blockType === "tool_result") {
          blockContent = typeof block.content === "string" ? block.content : JSON.stringify(block.content, null, 2);
        } else {
          blockContent = JSON.stringify(block, null, 2);
        }

        return (
          <div key={i} className={`rounded border ${style.border} ${style.bg} p-2`}>
            <div className="mb-1">
              <ContentBlockTag type={blockType} />
              {blockType === "tool_use" && block.name && (
                <span className="ml-1.5 text-[10px] font-mono text-cyan-300">{block.name}</span>
              )}
            </div>
            <pre className="whitespace-pre-wrap break-words text-xs text-gray-100 font-mono overflow-x-auto max-h-64 overflow-y-auto">
              {blockContent}
            </pre>
          </div>
        );
      })}
    </div>
  );
}

function ExpandedContent({ content, isLoading, error }) {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-400">
        <div className="skeleton h-4 w-24" />
        Loading content...
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-sm text-red-400">
        Error: {error}
      </div>
    );
  }

  if (!content) {
    return (
      <div className="text-sm text-gray-400">
        No content logged for this request.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header metadata */}
      {(content.response_model || content.stop_reason || content.model || content.provider) && (
        <div className="flex flex-wrap gap-1.5">
          <MetadataTag label="model" value={content.response_model || content.model} color="text-indigo-400" />
          <MetadataTag label="provider" value={content.provider} color="text-gray-400" />
          <MetadataTag label="stop" value={content.stop_reason} color="text-amber-400" />
          <MetadataTag label="source" value={content.source} color="text-gray-400" />
        </div>
      )}

      {/* Usage bar */}
      <UsageBar usage={content.usage} />

      {/* Messages */}
      {content.messages && content.messages.length > 0 ? (
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
            Messages ({content.messages.length})
          </div>
          <div className="space-y-2">
            {content.messages.map((msg, idx) => {
              const roleStyle = ROLE_STYLES[msg.role] || "bg-gray-500/25 text-gray-300 border-gray-500/40";
              return (
                <div
                  key={idx}
                  className={`rounded-md border p-2.5 ${roleStyle}`}
                >
                  <div className="mb-1.5 flex items-center gap-2">
                    <span className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider bg-black/20">
                      {msg.role}
                    </span>
                    {msg.role === "user" && typeof msg.content === "string" && msg.content.length > 200 && (
                      <span className="text-[10px] font-mono text-gray-400">
                        {msg.content.length.toLocaleString()} chars
                      </span>
                    )}
                  </div>
                  {renderContentBlocks(msg.content)}
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="text-sm text-gray-400">(no messages logged)</div>
      )}

      {/* Response — structured blocks if available, fall back to text */}
      {(content.response_content || content.response) && (
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
            Response
          </div>
          {content.response_content ? (
            renderContentBlocks(content.response_content)
          ) : (
            <div className="rounded-md bg-emerald-900/15 border border-emerald-500/30 p-2.5">
              <pre className="whitespace-pre-wrap break-words text-xs text-gray-100 font-mono overflow-x-auto">
                {typeof content.response === "string" ? content.response : JSON.stringify(content.response, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
