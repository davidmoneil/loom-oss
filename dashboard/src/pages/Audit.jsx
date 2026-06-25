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
              entries.map((e) => (
                <tr key={e.request_id} className="bg-base hover:bg-gray-800/40">
                  <Td className="whitespace-nowrap text-gray-400">
                    {fmtTime(e.timestamp)}
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
                </tr>
              ))
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
