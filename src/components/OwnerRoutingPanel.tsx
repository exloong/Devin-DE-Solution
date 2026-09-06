import { AlertTriangle, Check, ChevronRight, Clock3, X } from 'lucide-react';
import type { OwnerRouting } from '../api';

function initialsFor(team: string): string {
  return team
    .split(/[\s&/]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(part => part[0]?.toUpperCase() ?? '')
    .join('');
}

const reviewLabels = {
  accepted: { label: 'Review requested', Icon: Check, tone: 'ok' },
  pending: { label: 'Request pending', Icon: Clock3, tone: 'pending' },
  rejected: { label: 'GitHub rejected request', Icon: X, tone: 'bad' },
  not_requested: { label: 'Not requested', Icon: ChevronRight, tone: 'muted' },
} as const;

/** Deterministic reviewer routing for exloong/superset with the rule behind each candidate. */
export function OwnerRoutingPanel({ routing, compact = false }: { routing: OwnerRouting; compact?: boolean }) {
  const selected = routing.candidates.filter(candidate => candidate.selected);
  return (
    <div className={`owner-routing ${compact ? 'compact' : ''}`}>
      {routing.state !== 'resolved' && (
        <div className="owner-routing-warning" role="status">
          <AlertTriangle size={15} />
          <span>
            {routing.state === 'ambiguous'
              ? 'Ownership is ambiguous; the issue waits in needs_owner_decision instead of guessing.'
              : 'No eligible owner was found; the issue waits in needs_owner_decision.'}
            {routing.escalation ? ` Escalation: ${routing.escalation}.` : ''}
          </span>
        </div>
      )}
      {routing.candidates.length === 0 && <p className="owner-routing-empty">No routing candidates recorded for this revision.</p>}
      {routing.candidates.map(candidate => {
        const review = reviewLabels[candidate.review_requested ?? 'not_requested'];
        return (
          <div className={`owner-candidate ${candidate.selected ? 'selected' : ''}`} key={`${candidate.team}-${candidate.rule}`}>
            <span className="avatar small">{candidate.initials ?? initialsFor(candidate.team)}</span>
            <div>
              <strong>
                {candidate.team}
                {candidate.selected && <em>Selected</em>}
              </strong>
              <span>
                <code>{candidate.rule}</code> · {candidate.rationale}
              </span>
              {!compact && candidate.paths && candidate.paths.length > 0 && (
                <small>{candidate.paths.slice(0, 3).join(' · ')}{candidate.paths.length > 3 ? ` · +${candidate.paths.length - 3}` : ''}</small>
              )}
            </div>
            <span className={`owner-review-state ${review.tone}`}>
              <review.Icon size={12} /> {review.label}
            </span>
          </div>
        );
      })}
      {!compact && selected.length > 0 && (
        <p className="owner-routing-footnote">
          Requests go to <strong>exloong/superset</strong> reviewers derived from <code>.github/CODEOWNERS</code>, changed paths, and routing policy.
          Owner silence escalates; it never approves.
        </p>
      )}
    </div>
  );
}
