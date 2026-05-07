export function Brand() {
  return (
    <div className="flex items-baseline gap-3">
      <span
        className="font-display text-3xl font-light tracking-tightest text-parchment-50"
        style={{ fontVariationSettings: "'opsz' 144, 'SOFT' 40" }}
      >
        Iridium
      </span>
      <span className="font-mono text-[10px] uppercase tracking-widest text-parchment-400">
        / ops terminal
      </span>
    </div>
  );
}
