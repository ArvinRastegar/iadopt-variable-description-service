import { ChevronDownIcon, SlidersHorizontalIcon } from 'lucide-react';

import { Label } from '@/components/ui/fields';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/menus';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/controls';
import { Callout } from '@/components/ui/feedback';
import { useWorkspace } from '../state/workspace';

/** Model choice, tucked away: the default setup is the one with a measured accuracy. */
export function RunSettingsPopover() {
  const { modelOptions, runSettings, setRunSettings, decomposing } = useWorkspace();
  const providers = modelOptions?.providers ?? {};
  const providerIds = Object.keys(providers);
  const provider = providers[runSettings.provider];
  const defaultProvider = modelOptions ? providers[modelOptions.defaultProvider] : undefined;
  const isDefault =
    runSettings.provider === modelOptions?.defaultProvider &&
    runSettings.model === defaultProvider?.default_model_name;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={decomposing}
          className="flex h-[34px] min-w-0 items-center gap-2 rounded-md px-2.5 text-[13px] font-medium text-text-2 transition-colors outline-none hover:bg-surface-3 hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/40 disabled:opacity-50"
        >
          <SlidersHorizontalIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className="truncate">
            {runSettings.model || 'Default model'}
            {isDefault ? ' · default setup' : ''}
          </span>
          <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="flex w-[min(360px,calc(100vw-32px))] flex-col gap-4">
        <div className="text-sm font-semibold">Decomposition settings</div>
        {providerIds.length > 1 && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="run-provider">Provider</Label>
            <Select
              value={runSettings.provider}
              onValueChange={(value) => setRunSettings({
                ...runSettings,
                provider: value,
                model: providers[value]?.default_model_name ?? '',
              })}
            >
              <SelectTrigger id="run-provider"><SelectValue /></SelectTrigger>
              <SelectContent>
                {providerIds.map((id) => <SelectItem key={id} value={id}>{providers[id].label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        )}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="run-model">Model</Label>
          <Select value={runSettings.model} onValueChange={(value) => setRunSettings({ ...runSettings, model: value })}>
            <SelectTrigger id="run-model" aria-label="Model selection"><SelectValue placeholder="Default model" /></SelectTrigger>
            <SelectContent>
              {(provider?.model_names ?? []).map((name) => (
                <SelectItem key={name} value={name}>
                  {name}{name === provider?.default_model_name ? ' (default)' : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {!isDefault && (
          <Callout tone="warning">Only the default model has a measured accuracy. Other models give drafts of unknown quality.</Callout>
        )}
      </PopoverContent>
    </Popover>
  );
}
