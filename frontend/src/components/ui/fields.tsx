import * as React from 'react';
import { Label as LabelPrimitive } from 'radix-ui';

import { cn } from '@/lib/utils';

const fieldBase =
  'w-full min-w-0 rounded-md border border-input bg-surface px-3 text-sm text-foreground shadow-xs transition-[color,box-shadow,border-color] outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 read-only:bg-surface-2 read-only:text-text-2';

function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return <input type={type} data-slot="input" className={cn(fieldBase, 'h-9', className)} {...props} />;
}

function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return <textarea data-slot="textarea" className={cn(fieldBase, 'min-h-20 py-2 leading-5', className)} {...props} />;
}

function Label({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn('text-[13px] leading-5 font-medium text-text-2 select-none', className)}
      {...props}
    />
  );
}

/** Small uppercase heading used for panel sections. */
function SectionLabel({ className, ...props }: React.ComponentProps<'h2'>) {
  return (
    <h2
      className={cn('m-0 text-xs leading-4 font-semibold tracking-[0.05em] text-muted-foreground uppercase', className)}
      {...props}
    />
  );
}

function Kbd({ className, ...props }: React.ComponentProps<'kbd'>) {
  return (
    <kbd
      className={cn(
        'inline-flex h-5 items-center rounded-[5px] border border-border bg-surface-2 px-1.5 font-mono text-[11px] font-medium text-muted-foreground',
        className,
      )}
      {...props}
    />
  );
}

export { Input, Kbd, Label, SectionLabel, Textarea };
