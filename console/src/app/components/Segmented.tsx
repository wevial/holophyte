import { useTheme } from "../appearance";

export interface SegmentedOption<V extends string> {
  value: V;
  label: string;
  disabled?: boolean;
  title?: string;
}

/** A group named `label` of one pressed button per option, the pressed
 *  one `value`; a `value` matching no option presses none. */
export function Segmented<V extends string>({ label, options, value, onPick, disabled, title }: {
  label: string; options: readonly SegmentedOption<V>[]; value: V | undefined; onPick: (picked: V) => void;
  disabled?: boolean; title?: string;
}) {
  const light = useTheme().resolvedTheme === "light";
  const track = light ? "bg-selected" : "bg-raised";
  const picked = light ? "aria-pressed:bg-raised aria-pressed:shadow-[0_1px_2px_rgb(29_24_43/0.12)]" : "aria-pressed:bg-selected";
  return (
    <div role="group" aria-label={label} className={`inline-flex shrink-0 rounded-[7px] p-0.5 shadow-[inset_0_0_0_1px_var(--hair)] ${track}`}>
      {options.map((option) => (
        <button key={option.value} type="button" aria-pressed={option.value === value} disabled={disabled || option.disabled} title={option.title ?? title} onClick={() => onPick(option.value)}
          className={`inline-flex h-[28px] cursor-pointer items-center gap-1.5 rounded-[5px] px-3 font-sans text-[12.5px] font-medium leading-none whitespace-nowrap text-ink-3 transition-colors hover:text-ink aria-pressed:text-ink disabled:cursor-not-allowed disabled:opacity-40 ${picked}`}>
          {option.label}
        </button>
      ))}
    </div>
  );
}
