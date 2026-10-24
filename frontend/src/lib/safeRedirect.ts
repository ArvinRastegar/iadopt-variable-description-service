/**
 * The page to return to after signing in. Only same-site paths are allowed, so a crafted
 * `?redirect=https://elsewhere` link cannot send people to another site after they sign in.
 */
export function safeRedirect(value: string | null | undefined, fallback = '/'): string {
  if (!value) return fallback;
  const trimmed = value.trim();
  if (!trimmed.startsWith('/') || trimmed.startsWith('//') || trimmed.startsWith('/\\')) return fallback;
  try {
    const url = new URL(trimmed, 'https://same-site.invalid');
    if (url.origin !== 'https://same-site.invalid') return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}
