/** The iD as people type it, without the https://orcid.org/ prefix. */
export function bareOrcid(uri: string): string {
  return uri.replace(/^https?:\/\/orcid\.org\//i, '');
}
