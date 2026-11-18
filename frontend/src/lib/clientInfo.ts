import type { ClientInfo } from '@/shared/api';

/*
 * The device and screen the browser reports, sent with each recorded step of a signed-in account
 * (D-037); the server adds the IP address and the browser string itself. A value outside the
 * backend's bounds is left out, because the backend drops the whole client info when one field
 * does not fit.
 */

type UserAgentData = { platform?: unknown; mobile?: unknown };

function whole(value: unknown, max: number): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= max ? Math.round(value) : undefined;
}

function ratio(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 100 ? Math.round(value * 1000) / 1000 : undefined;
}

function defined<T extends object>(value: T): T | undefined {
  const entries = Object.entries(value).filter(([, item]) => item !== undefined);
  return entries.length ? (Object.fromEntries(entries) as T) : undefined;
}

/** The screen and device of this browser, or undefined where it reports nothing usable. */
export function clientInfo(): ClientInfo | undefined {
  try {
    const nav = globalThis.navigator as (Navigator & { userAgentData?: UserAgentData }) | undefined;
    const screenInfo = globalThis.screen;
    const agentData = nav?.userAgentData;
    const platform = typeof agentData?.platform === 'string' && agentData.platform ? agentData.platform : nav?.platform;
    const client = defined<ClientInfo>({
      screen: screenInfo
        ? defined({
          width: whole(screenInfo.width, 100_000),
          height: whole(screenInfo.height, 100_000),
          pixel_ratio: ratio(globalThis.devicePixelRatio),
        })
        : undefined,
      device: defined({
        platform: typeof platform === 'string' && platform ? Array.from(platform).slice(0, 100).join('') : undefined,
        mobile: typeof agentData?.mobile === 'boolean' ? agentData.mobile : undefined,
        touch_points: whole(nav?.maxTouchPoints, 1_000),
      }),
    });
    return client;
  } catch {
    return undefined;
  }
}
