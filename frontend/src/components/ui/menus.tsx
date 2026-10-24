import * as React from 'react';
import { DropdownMenu as MenuPrimitive, Popover as PopoverPrimitive, Tooltip as TooltipPrimitive } from 'radix-ui';

import { cn } from '@/lib/utils';

const floatingPanel =
  'z-50 rounded-xl border border-border bg-popover text-popover-foreground shadow-md outline-none data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[side=bottom]:slide-in-from-top-1 data-[side=top]:slide-in-from-bottom-1';

/* ----------------------------- Dropdown menu ----------------------------- */

const DropdownMenu = MenuPrimitive.Root;
const DropdownMenuTrigger = MenuPrimitive.Trigger;
const DropdownMenuGroup = MenuPrimitive.Group;

function DropdownMenuContent({ className, sideOffset = 6, ...props }: React.ComponentProps<typeof MenuPrimitive.Content>) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Content sideOffset={sideOffset} className={cn(floatingPanel, 'min-w-48 p-1.5', className)} {...props} />
    </MenuPrimitive.Portal>
  );
}

function DropdownMenuItem({
  className,
  variant = 'default',
  ...props
}: React.ComponentProps<typeof MenuPrimitive.Item> & { variant?: 'default' | 'destructive' }) {
  return (
    <MenuPrimitive.Item
      data-variant={variant}
      className={cn(
        "relative flex min-h-9 cursor-default items-center gap-2 rounded-md px-2.5 text-sm outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 data-[highlighted]:bg-surface-2 data-[variant=destructive]:text-destructive-soft-foreground [&_svg:not([class*='size-'])]:size-4 [&_svg]:shrink-0",
        className,
      )}
      {...props}
    />
  );
}

function DropdownMenuLabel({ className, ...props }: React.ComponentProps<typeof MenuPrimitive.Label>) {
  return <MenuPrimitive.Label className={cn('px-2.5 py-1.5 text-xs font-semibold text-muted-foreground', className)} {...props} />;
}

function DropdownMenuSeparator({ className, ...props }: React.ComponentProps<typeof MenuPrimitive.Separator>) {
  return <MenuPrimitive.Separator className={cn('mx-1.5 my-1 h-px bg-border', className)} {...props} />;
}

/* -------------------------------- Popover -------------------------------- */

const Popover = PopoverPrimitive.Root;
const PopoverTrigger = PopoverPrimitive.Trigger;
const PopoverAnchor = PopoverPrimitive.Anchor;

function PopoverContent({ className, align = 'start', sideOffset = 6, ...props }: React.ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content align={align} sideOffset={sideOffset} className={cn(floatingPanel, 'w-80 p-4', className)} {...props} />
    </PopoverPrimitive.Portal>
  );
}

/* -------------------------------- Tooltip -------------------------------- */

function TooltipProvider({ delayDuration = 300, ...props }: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return <TooltipPrimitive.Provider delayDuration={delayDuration} {...props} />;
}

const Tooltip = TooltipPrimitive.Root;
const TooltipTrigger = TooltipPrimitive.Trigger;

function TooltipContent({ className, sideOffset = 6, children, ...props }: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        sideOffset={sideOffset}
        className={cn(
          'z-50 max-w-72 rounded-md bg-foreground px-2.5 py-1.5 text-xs leading-4 text-background shadow-md data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0',
          className,
        )}
        {...props}
      >
        {children}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  );
}

export {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
};
