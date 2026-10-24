import * as React from 'react';
import { Dialog as SheetPrimitive } from 'radix-ui';
import { XIcon } from 'lucide-react';

import { cn } from '@/lib/utils';

function Sheet(props: React.ComponentProps<typeof SheetPrimitive.Root>) {
  return <SheetPrimitive.Root data-slot="sheet" {...props} />;
}

function SheetContent({ className, children, ...props }: React.ComponentProps<typeof SheetPrimitive.Content>) {
  return (
    <SheetPrimitive.Portal>
      <SheetPrimitive.Overlay
        className="fixed inset-0 z-50 bg-(--backdrop) data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0"
      />
      <SheetPrimitive.Content
        data-slot="sheet-content"
        className={cn(
          'fixed inset-y-0 right-0 z-50 flex h-full w-full max-w-[460px] flex-col border-l border-border bg-surface text-foreground shadow-lg outline-none data-[state=closed]:animate-out data-[state=closed]:slide-out-to-right data-[state=open]:animate-in data-[state=open]:slide-in-from-right',
          className,
        )}
        {...props}
      >
        {children}
        <SheetPrimitive.Close className="absolute top-4 right-4 inline-flex size-8 items-center justify-center rounded-md text-text-2 transition-colors outline-none hover:bg-surface-2 hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/40">
          <XIcon className="size-4" />
          <span className="sr-only">Close</span>
        </SheetPrimitive.Close>
      </SheetPrimitive.Content>
    </SheetPrimitive.Portal>
  );
}

function SheetHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('flex flex-col gap-1 px-6 pt-5 pr-14', className)} {...props} />;
}

function SheetFooter({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn('mt-auto flex flex-wrap justify-end gap-2 border-t border-border bg-surface-2 px-6 py-4', className)}
      {...props}
    />
  );
}

function SheetTitle({ className, ...props }: React.ComponentProps<typeof SheetPrimitive.Title>) {
  return (
    <SheetPrimitive.Title
      className={cn('m-0 text-[19px] leading-[25px] font-semibold tracking-[-0.01em]', className)}
      {...props}
    />
  );
}

function SheetDescription({ className, ...props }: React.ComponentProps<typeof SheetPrimitive.Description>) {
  return <SheetPrimitive.Description className={cn('m-0 text-[13px] text-muted-foreground', className)} {...props} />;
}

export { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle };
