import { useCallback, useState } from "react";

// Cap how far back a single relative window can reach — generous enough
// for any real use (1 year in either unit), just guards against a typo
// like an extra zero turning into a multi-year query.
const MAX_HOURS = 8760;
const MAX_DAYS = 365;

// Shared relative time-range state, used by every page that windows its
// data by "last N hours/days" — keeps the control (and its clamping/
// quick-pick behavior) identical across pages instead of each page
// re-implementing its own fixed range-button list.
export function useTimeRange({ defaultAmount, defaultUnit }) {
  const [amount, setAmount] = useState(defaultAmount);
  const [amountInput, setAmountInput] = useState(String(defaultAmount));
  const [unit, setUnit] = useState(defaultUnit);

  const commitAmount = useCallback(() => {
    const max = unit === "days" ? MAX_DAYS : MAX_HOURS;
    const parsed = Math.round(Number(amountInput));
    const clamped = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), max) : amount;
    setAmount(clamped);
    setAmountInput(String(clamped));
  }, [amountInput, amount, unit]);

  const setQuickPick = useCallback((qp) => {
    setAmount(qp.amount);
    setAmountInput(String(qp.amount));
    setUnit(qp.unit);
  }, []);

  const hours = unit === "days" ? amount * 24 : amount;
  const days = Math.max(1, Math.ceil(hours / 24));
  const rangeLabel = `${amount}${unit === "days" ? "d" : "h"}`;

  return {
    amount,
    amountInput,
    unit,
    hours,
    days,
    rangeLabel,
    setAmountInput,
    commitAmount,
    setUnit,
    setQuickPick,
    isQuickPick: (qp) => amount === qp.amount && unit === qp.unit,
    maxForUnit: unit === "days" ? MAX_DAYS : MAX_HOURS,
  };
}

export default function TimeRangeControl({ range, quickPicks }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm text-gray-400">Last</span>
      <input
        type="number"
        inputMode="numeric"
        min={1}
        max={range.maxForUnit}
        aria-label="Time range amount"
        value={range.amountInput}
        onChange={(e) => range.setAmountInput(e.target.value)}
        onBlur={range.commitAmount}
        onKeyDown={(e) => e.key === "Enter" && range.commitAmount()}
        className="w-16 rounded-md border border-border bg-card px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-accent"
      />
      <select
        aria-label="Time range unit"
        value={range.unit}
        onChange={(e) => range.setUnit(e.target.value)}
        className="rounded-md border border-border bg-card px-2 py-1.5 text-sm text-gray-300 hover:bg-gray-700/50 focus:outline-none focus:ring-1 focus:ring-accent"
      >
        <option value="hours">Hours</option>
        <option value="days">Days</option>
      </select>
      <div className="flex overflow-hidden rounded-md border border-border">
        {quickPicks.map((qp) => (
          <button
            key={qp.label}
            onClick={() => range.setQuickPick(qp)}
            className={`px-2.5 py-1.5 text-xs ${
              range.isQuickPick(qp)
                ? "bg-accent text-white"
                : "bg-card text-gray-400 hover:bg-gray-700/50"
            }`}
          >
            {qp.label}
          </button>
        ))}
      </div>
    </div>
  );
}
