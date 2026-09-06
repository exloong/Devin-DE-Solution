import { ShieldAlert } from 'lucide-react';
import { RepositorySafetyError, TARGET_REPOSITORY, type ApiError } from '../api';

/**
 * Shown when a record references a repository other than the configured
 * Superset target. Such records must never enter the flow.
 */
export function RepositorySafetyNotice({ error }: { error: ApiError }) {
  if (!(error instanceof RepositorySafetyError)) return null;
  return (
    <div className="repository-safety" role="alert">
      <ShieldAlert size={20} />
      <div>
        <strong>Wrong repository blocked</strong>
        <span>
          A record for <code>{error.repository}</code> reached the dashboard. Relay is configured for <code>{TARGET_REPOSITORY}</code> only, so
          the record was rejected before any lifecycle, session, or pull-request action.
        </span>
      </div>
    </div>
  );
}

export function isRepositorySafetyError(error: unknown): error is RepositorySafetyError {
  return error instanceof RepositorySafetyError;
}
