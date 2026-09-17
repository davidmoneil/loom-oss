// Small "i" icon that reveals a description on hover/focus. Pure CSS
// popover (no JS state, no extra dependency) so it matches the rest of
// this dashboard's zero-UI-library approach.
export default function WidgetInfo({ text }) {
  if (!text) return null;
  return (
    <span className="group relative inline-flex">
      <button
        type="button"
        aria-label="Widget info"
        className="flex h-4 w-4 items-center justify-center rounded-full text-gray-500 outline-none hover:text-gray-300 focus:text-gray-300"
      >
        <svg viewBox="0 0 16 16" fill="none" className="h-3.5 w-3.5">
          <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.3" />
          <path d="M8 7.25v4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          <circle cx="8" cy="4.85" r="0.9" fill="currentColor" />
        </svg>
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute left-1/2 top-full z-20 mt-2 w-56 -translate-x-1/2 rounded-lg border border-border bg-[#1f2937] p-2.5 text-xs font-normal normal-case leading-snug text-gray-200 opacity-0 shadow-lg transition-opacity duration-100 group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}
