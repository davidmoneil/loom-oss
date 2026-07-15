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
  const [filters, setFilters] = useState({
    search: "",
    model: "",
    source: "",
    status: "",
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
                setContentError((prev) => ({ ...prev, [requestId]: e.message }));
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
      </div>

      <div className="mt-4 overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[1000px] text-left text-sm">
          <thead className="bg-card text-xs uppercase tracking-wide text-gray-400">
            <tr>
              <Th>Time</Th>
              <Th>Source</Th>
              <Th>Requested</Th>
              <Th>Routed To</Th>
              <Th>Task</Th>
              <Th className="text-right">Tokens (in/out)</Th>
              <Th className="text-right">Latency</Th>
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
                <td colSpan={10} className="p-8 text-center text-gray-500">
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
                  <Td className="text-gray-400">{e.requested_model}</Td>
                  <Td className="font-medium text-gray-100">{e.model_used}</Td>
                  <Td>{e.task_type}</Td>
                  <Td className="text-right tabular-nums text-gray-300">
                    {fmtNumber(e.tokens_in)} / {fmtNumber(e.tokens_out)}
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
                    <td colSpan={10} className="px-3 py-3">
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
      {Array.from({ length: 10 }).map((__, j) => (
        <td key={j} className="px-3 py-3">
          <div className="skeleton h-4 w-full" />
        </td>
      ))}
    </tr>
  ));
}

function renderMessageContent(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((block) =>
        block && typeof block === "object" && typeof block.text === "string"
          ? block.text
          : JSON.stringify(block, null, 2)
      )
      .join("\n");
  }
  return JSON.stringify(value, null, 2);
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
      {content.messages && content.messages.length > 0 ? (
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
            Messages
          </div>
          <div className="space-y-2">
            {content.messages.map((msg, idx) => (
              <div
                key={idx}
                className="rounded bg-gray-800/40 p-2 border border-border/30"
              >
                <div className="mb-1 text-xs font-medium text-gray-300">
                  {msg.role}
                </div>
                <pre className="whitespace-pre-wrap break-words text-xs text-gray-200 font-mono overflow-x-auto">
                  {renderMessageContent(msg.content)}
                </pre>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="text-sm text-gray-400">(no messages logged)</div>
      )}

      {content.response && (
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
            Response
          </div>
          <div className="rounded bg-gray-800/40 p-2 border border-border/30">
            <pre className="whitespace-pre-wrap break-words text-xs text-gray-200 font-mono overflow-x-auto">
              {renderMessageContent(content.response)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
