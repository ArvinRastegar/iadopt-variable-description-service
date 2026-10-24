import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { Toaster as SonnerToaster, type ToasterProps } from 'sonner';
import { AlertTriangleIcon, CircleCheckIcon, InfoIcon, OctagonAlertIcon } from 'lucide-react';

import { cn } from '@/lib/utils';

/* ---------------------------------- Pill --------------------------------- */

const pillVariants = cva(
  "inline-flex h-[22px] shrink-0 items-center gap-1.5 rounded-full px-2 text-xs leading-4 font-semibold whitespace-nowrap [&_svg:not([class*='size-'])]:size-3",
  {
    variants: {
      tone: {
        neutral: 'bg-surface-3 text-text-2',
        success: 'bg-primary-soft text-primary-soft-foreground',
        warning: 'border border-warning-line bg-warning-soft text-warning-foreground',
        danger: 'border border-destructive-line bg-destructive-soft text-destructive-soft-foreground',
        info: 'border border-info-line bg-info-soft text-info-foreground',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
);

/** A status pill. Always carries a dot or icon plus words, never colour alone. */
function Pill({
  className,
  tone,
  dot = false,
  children,
  ...props
}: React.ComponentProps<'span'> & VariantProps<typeof pillVariants> & { dot?: boolean }) {
  return (
    <span className={cn(pillVariants({ tone }), className)} {...props}>
      {dot && <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/* -------------------------------- Callout -------------------------------- */

const calloutVariants = cva('flex gap-3 rounded-xl border px-4 py-3 text-[13px] leading-[19px]', {
  variants: {
    tone: {
      info: 'border-info-line bg-info-soft text-info-foreground',
      warning: 'border-warning-line bg-warning-soft text-warning-foreground',
      danger: 'border-destructive-line bg-destructive-soft text-destructive-soft-foreground',
      success: 'border-transparent bg-primary-soft text-primary-soft-foreground',
      neutral: 'border-transparent bg-surface-2 text-text-2',
    },
  },
  defaultVariants: { tone: 'info' },
});

const calloutIcons = {
  info: InfoIcon,
  warning: AlertTriangleIcon,
  danger: OctagonAlertIcon,
  success: CircleCheckIcon,
  neutral: InfoIcon,
};

function Callout({
  className,
  tone = 'info',
  title,
  children,
  icon = true,
  ...props
}: Omit<React.ComponentProps<'div'>, 'title'> &
  VariantProps<typeof calloutVariants> & { title?: React.ReactNode; icon?: boolean }) {
  const Icon = calloutIcons[tone ?? 'info'];
  return (
    <div className={cn(calloutVariants({ tone }), className)} {...props}>
      {icon && <Icon aria-hidden="true" className="mt-px size-4 shrink-0" />}
      <div className="min-w-0 flex-1">
        {title && <div className="mb-0.5 text-sm font-semibold">{title}</div>}
        {children}
      </div>
    </div>
  );
}

/* --------------------------------- Toasts -------------------------------- */

function Toaster(props: ToasterProps) {
  return (
    <SonnerToaster
      position="bottom-center"
      toastOptions={{
        classNames: {
          toast:
            '!rounded-xl !border-border !bg-foreground !text-background !shadow-md !font-sans !text-[13px] !gap-3 !px-4 !py-3',
          description: '!text-background/75',
          actionButton: '!bg-background !text-foreground !font-semibold',
          error: '!bg-destructive !text-destructive-foreground !border-transparent',
        },
      }}
      {...props}
    />
  );
}

/* -------------------------------- Spinner -------------------------------- */

function Spinner({ className, ...props }: React.ComponentProps<'svg'>) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={cn('size-4 animate-spin', className)}
      fill="none"
      stroke="currentColor"
      strokeWidth={2.4}
      strokeLinecap="round"
      {...props}
    >
      <path d="M12 3a9 9 0 1 0 9 9" />
    </svg>
  );
}

export { Callout, Pill, Spinner, Toaster };
