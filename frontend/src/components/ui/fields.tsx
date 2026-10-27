import * as React from 'react';
import { Label as LabelPrimitive } from 'radix-ui';
import { EyeIcon, EyeOffIcon } from 'lucide-react';

import { cn } from '@/lib/utils';

const fieldBase =
  'w-full min-w-0 rounded-md border border-input bg-surface px-3 text-sm text-foreground shadow-xs transition-[color,box-shadow,border-color] outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 read-only:bg-surface-2 read-only:text-text-2';

function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return <input type={type} data-slot="input" className={cn(fieldBase, 'h-9', className)} {...props} />;
}

/**
 * Password field with an eye button that shows or hides what was typed. Spelling and
 * autocorrection stay off, so a shown password is never sent to a spelling service.
 */
function PasswordInput({ className, ...props }: Omit<React.ComponentProps<'input'>, 'type'>) {
  const [visible, setVisible] = React.useState(false);
  return (
    <div className="relative">
      <Input
        type={visible ? 'text' : 'password'}
        spellCheck={false}
        autoCapitalize="none"
        autoCorrect="off"
        className={cn('block pr-10', className)}
        {...props}
      />
      <button
        type="button"
        aria-controls={props.id}
        disabled={props.disabled}
        onClick={() => setVisible((current) => !current)}
        className="absolute inset-y-0 right-0 flex w-9 items-center justify-center rounded-r-md text-muted-foreground transition-colors outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:pointer-events-none disabled:opacity-50"
      >
        {visible ? <EyeOffIcon aria-hidden="true" className="size-4" /> : <EyeIcon aria-hidden="true" className="size-4" />}
        {/* Text rather than aria-label, so the button never counts as a label of the field. */}
        <span className="sr-only">{visible ? 'Hide password' : 'Show password'}</span>
      </button>
    </div>
  );
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

export { Input, Kbd, Label, PasswordInput, SectionLabel, Textarea };
