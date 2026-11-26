const MAX_LENGTH = 2_048;

/** `/` and at most one name, which has a letter or digit; then printable ASCII without a space or backslash. */
const TARGET = /^\/([._-]*[A-Za-z0-9][A-Za-z0-9._-]*)?(?:[?#][\x21-\x5b\x5d-\x7e]*)?$/;

/** Names that are no page to return to. */
const NO_PAGES = ['api', 'login.html'];

/**
 * The page to return to after signing in: `/`, or `/` and one name such as `admin.html`, with an
 * optional query and fragment. Anything else gives the fallback, so a crafted `?redirect=` link
 * cannot send people to another site after they sign in. The value is judged and returned as it
 * stands: normalizing `/.//elsewhere` first would leave `//elsewhere`, which is another site.
 */
export function safeRedirect(value: string | null | undefined, fallback = '/'): string {
  if (!value || value.length > MAX_LENGTH) return fallback;
  const target = TARGET.exec(value);
  return target && !NO_PAGES.includes(target[1]) ? value : fallback;
}
