import React, { useCallback, useMemo, useRef } from "react";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

const LINE_CLASS = "leading-[1.625rem] h-[1.625rem]";

type Props = {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
  minHeightClass?: string;
};

export function NumberedPayloadEditor({
  value,
  onChange,
  disabled,
  placeholder,
  className,
  minHeightClass = "min-h-[240px]",
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);

  const lineCount = useMemo(() => {
    if (!value) return 1;
    return value.split("\n").length;
  }, [value]);

  const lineNumbers = useMemo(
    () => Array.from({ length: lineCount }, (_, i) => i + 1),
    [lineCount],
  );

  const syncScroll = useCallback(() => {
    const ta = textareaRef.current;
    const gutter = gutterRef.current;
    if (ta && gutter) gutter.scrollTop = ta.scrollTop;
  }, []);

  return (
    <div
      className={cn(
        "flex rounded-md border border-border overflow-hidden bg-background/50 font-mono text-sm",
        minHeightClass,
        className,
      )}
    >
      <div
        ref={gutterRef}
        aria-hidden
        className="shrink-0 overflow-hidden border-r border-border/60 bg-muted/25 px-2 py-2 text-[11px] text-muted-foreground select-none tabular-nums min-w-[2.5rem] text-right"
      >
        {lineNumbers.map(n => (
          <div key={n} className={LINE_CLASS}>
            {n}
          </div>
        ))}
      </div>
      <Textarea
        ref={textareaRef}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        onScroll={syncScroll}
        spellCheck={false}
        className={cn(
          "flex-1 resize-y rounded-none border-0 shadow-none focus-visible:ring-0 focus-visible:ring-offset-0",
          "bg-transparent py-2 px-3 leading-[1.625rem] min-h-0 h-full",
        )}
        rows={Math.max(lineCount, 8)}
      />
    </div>
  );
}
