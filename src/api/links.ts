import { TARGET_REPOSITORY } from './types';

/**
 * Link validation for URLs supplied by the API. Every rendered anchor goes
 * through `validateLink`, so a compromised or misconfigured backend cannot
 * point the operator at an arbitrary host, a non-HTTPS origin, or a GitHub
 * repository other than the configured Superset target.
 */

export type LinkPolicy = 'github' | 'devin' | 'artifact';

export type LinkCheck = { ok: true; href: string } | { ok: false; reason: string; href: string | null };

export const GITHUB_HOST = 'github.com';
export const DEVIN_HOSTS = ['app.devin.ai', 'devin.ai'] as const;
const DEVIN_PATH_PREFIXES = ['/sessions/', '/review/', '/workspace/'] as const;

export const TARGET_REPOSITORY_URL = `https://${GITHUB_HOST}/${TARGET_REPOSITORY}`;

function parse(url: string): URL | null {
  try {
    return new URL(url);
  } catch {
    return null;
  }
}

function isDevinHost(host: string): boolean {
  return DEVIN_HOSTS.some(allowed => host === allowed || host.endsWith(`.${allowed}`));
}

export function validateLink(url: string | null | undefined, policy: LinkPolicy): LinkCheck {
  if (!url) return { ok: false, reason: 'No link supplied by the API', href: null };
  const parsed = parse(url);
  if (!parsed) return { ok: false, reason: 'Link is not a valid URL', href: url };
  if (parsed.protocol !== 'https:') return { ok: false, reason: 'Link is not HTTPS', href: url };
  if (parsed.username || parsed.password) return { ok: false, reason: 'Link embeds credentials', href: url };

  switch (policy) {
    case 'github': {
      if (parsed.host !== GITHUB_HOST) return { ok: false, reason: `Link host ${parsed.host} is not ${GITHUB_HOST}`, href: url };
      const repoPath = `/${TARGET_REPOSITORY}`;
      if (parsed.pathname !== repoPath && !parsed.pathname.startsWith(`${repoPath}/`)) {
        return { ok: false, reason: `Link does not target ${TARGET_REPOSITORY}`, href: url };
      }
      return { ok: true, href: parsed.toString() };
    }
    case 'devin': {
      if (!isDevinHost(parsed.host)) return { ok: false, reason: `Link host ${parsed.host} is not an approved Devin host`, href: url };
      if (!DEVIN_PATH_PREFIXES.some(prefix => parsed.pathname.startsWith(prefix))) {
        return { ok: false, reason: 'Link is not a canonical Devin session, desktop, or review path', href: url };
      }
      return { ok: true, href: parsed.toString() };
    }
    case 'artifact':
      return { ok: true, href: parsed.toString() };
  }
}

/** Returns the validated href or null; use when a plain string is needed (e.g. to build commit/branch URLs). */
export function safeHref(url: string | null | undefined, policy: LinkPolicy): string | null {
  const check = validateLink(url, policy);
  return check.ok ? check.href : null;
}
