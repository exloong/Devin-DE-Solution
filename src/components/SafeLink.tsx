import { ShieldAlert } from 'lucide-react';
import type { ReactNode } from 'react';
import { validateLink, type LinkPolicy } from '../api/links';

/**
 * Renders an external anchor only when the API-supplied URL passes the link
 * policy. Unsafe links are omitted and replaced by an inline safety
 * disclosure so the operator can see that something was withheld.
 */
export function SafeLink({
  href,
  policy,
  className,
  title,
  children,
  fallback,
}: {
  href: string | null | undefined;
  policy: LinkPolicy;
  className?: string;
  title?: string;
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const check = validateLink(href, policy);
  if (check.ok) {
    return (
      <a className={className} href={check.href} target="_blank" rel="noreferrer noopener" title={title}>
        {children}
      </a>
    );
  }
  if (fallback !== undefined) return <>{fallback}</>;
  return (
    <span className={`unsafe-link ${className ?? ''}`} role="note" title={check.href ?? undefined}>
      <ShieldAlert size={13} /> Link withheld · {check.reason}
    </span>
  );
}
