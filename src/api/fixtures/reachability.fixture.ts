/**
 * Executable fixture (no test runner is installed in this repo):
 *
 *   npm run test:fixtures
 *
 * Bundles this file with esbuild and runs it under Node. It proves that
 * production fails closed when no API answers, demo fallback is opt-in, and
 * every reachable failure resolves to an explicit `error` state. It also
 * covers link policy, invalid-time formatting and session-status mapping.
 */
import { ApiClient, ApiError, RepositorySafetyError } from '../client';
import { validateLink } from '../links';
import { resolveFailureState, toApiError } from '../../hooks/useResource';
import { formatClock, formatDateTime, formatRelative, mapApiStatus, normalizeProgress, parseTime } from '../../hooks/sessionView';
import type { ResourceState } from '../../hooks/useResource';

Object.assign(globalThis, { window: globalThis });

let failures = 0;
function check(name: string, condition: boolean, detail?: unknown): void {
  if (condition) {
    console.log(`ok   ${name}`);
  } else {
    failures += 1;
    console.error(`FAIL ${name}`, detail === undefined ? '' : JSON.stringify(detail));
  }
}

type Reply = { status: number; contentType?: string; body: string } | Error;

function clientReplying(reply: Reply): ApiClient {
  const fetchImpl: typeof fetch = async () => {
    if (reply instanceof Error) throw reply;
    return new Response(reply.body, {
      status: reply.status,
      headers: { 'content-type': reply.contentType ?? 'application/json', 'x-correlation-id': 'fixture-corr' },
    });
  };
  return new ApiClient({ baseUrl: 'https://relay.test/api/v1', fetchImpl, timeoutMs: 1000 });
}

async function failureOf(run: () => Promise<unknown>): Promise<ApiError> {
  try {
    await run();
  } catch (raw) {
    return toApiError(raw);
  }
  throw new Error('expected the call to fail');
}

const loading: ResourceState<unknown> = { kind: 'loading' };

function sessionPage(repository: string): string {
  return JSON.stringify({
    items: [
      {
        id: '11111111-2222-3333-4444-555555555555',
        version: 3,
        issue_id: 'issue-1',
        issue_key: 'superset#100',
        issue_title: 'Chart fails to render',
        title: 'Reproduce superset#100',
        transition: 'reproducing',
        actor: 'devin',
        status: 'completed',
        created_at: '2026-09-05T00:00:00Z',
        budget: { wall_seconds: 3600, retry_limit: 1, retries_used: 0, allowed_capabilities: [], max_output_bytes: 1 },
        repository: { full_name: repository, html_url: `https://github.com/${repository}`, dry_run: false },
        target_commit: 'abcdef1234567890',
        workspace: { released: true },
        trigger: 'issue.opened',
        correlation_id: 'corr-1',
      },
    ],
    total: 1,
    generated_at: '2026-09-05T00:00:00Z',
  });
}

async function main(): Promise<void> {
  // 1. Wrong repository → reachable error, never demo.
  const wrongRepo = await failureOf(() => clientReplying({ status: 200, body: sessionPage('other/repo') }).listSessions());
  check('wrong repository raises RepositorySafetyError', wrongRepo instanceof RepositorySafetyError, wrongRepo);
  check('wrong repository is reachable', wrongRepo.reachable && !wrongRepo.apiAbsent);
  check('wrong repository resolves to error, not demo', resolveFailureState(loading, wrongRepo).kind === 'error');

  // 2. Malformed live payload (JSON content-type, unparsable body) → error.
  const malformed = await failureOf(() => clientReplying({ status: 200, body: '{not json' }).listSessions());
  check('malformed JSON maps to malformed_response', malformed.code === 'malformed_response', malformed);
  check('malformed JSON resolves to error, not demo', resolveFailureState(loading, malformed).kind === 'error');

  // 3. Schema-shaped payload that breaks mapping (items missing) → error via toApiError.
  const badShape = await failureOf(() => clientReplying({ status: 200, body: '{"total":1}' }).listSessions());
  check('schema mapping exception is reachable', badShape.reachable, badShape);
  check('schema mapping exception resolves to error', resolveFailureState(loading, badShape).kind === 'error');

  // 4. 401 with nested envelope → unauthorized error, message/details parsed.
  const unauthorized = await failureOf(() =>
    clientReplying({ status: 401, body: JSON.stringify({ error: { code: 'unauthorized', message: 'Session expired', details: { realm: 'relay' } } }) }).listSessions(),
  );
  check('401 maps to unauthorized', unauthorized.code === 'unauthorized' && unauthorized.message === 'Session expired', unauthorized);
  check('401 keeps details and correlation id', JSON.stringify(unauthorized.details) === '{"realm":"relay"}' && unauthorized.correlationId === 'fixture-corr');
  check('401 resolves to error, not demo', resolveFailureState(loading, unauthorized).kind === 'error');

  // 5. Top-level envelope form is also parsed.
  const policy = await failureOf(() => clientReplying({ status: 451, body: JSON.stringify({ code: 'policy_rejected', message: 'Blocked by policy' }) }).listSessions());
  check('top-level envelope parsed', policy.code === 'policy_rejected' && policy.message === 'Blocked by policy', policy);

  // 6. Genuinely absent API → production error; explicit mockup → demo.
  const network = await failureOf(() => clientReplying(new TypeError('fetch failed')).listSessions());
  check('network failure is apiAbsent', network.apiAbsent);
  check('network failure fails closed by default', resolveFailureState(loading, network).kind === 'error');
  check('network failure resolves to demo only when enabled', resolveFailureState(loading, network, true).kind === 'demo');
  const html404 = await failureOf(() => clientReplying({ status: 404, contentType: 'text/html', body: '<!doctype html>' }).listSessions());
  check('HTML 404 (no API mounted) fails closed', resolveFailureState(loading, html404).kind === 'error');

  // 7. Previously live data stays visible as stale on any failure.
  const live: ResourceState<number> = { kind: 'live', data: 1, fetchedAt: 1, stale: false };
  check('live → stale on failure', resolveFailureState(live, unauthorized).kind === 'stale');

  // 8. Link policy.
  check('desktop path allowed', validateLink('https://app.devin.ai/desktop/session/abc', 'devin').ok);
  check('session path allowed', validateLink('https://app.devin.ai/sessions/abc', 'devin').ok);
  check('workspace path withheld', !validateLink('https://app.devin.ai/workspace/abc', 'devin').ok);
  check('non-Devin host withheld', !validateLink('https://evil.example/sessions/abc', 'devin').ok);
  check('http withheld', !validateLink('http://app.devin.ai/sessions/abc', 'devin').ok);
  check('target repo PR allowed', validateLink('https://github.com/exloong/superset/pull/1', 'github').ok);
  check('other repo withheld', !validateLink('https://github.com/exloong/superset-fork/pull/1', 'github').ok);

  // 9. Invalid timestamps and unavailable progress.
  check('formatDateTime invalid → Unknown', formatDateTime('not-a-date') === 'Unknown');
  check('formatClock invalid → Unknown', formatClock(undefined) === 'Unknown');
  check('formatRelative null → Unknown', formatRelative(parseTime('garbage'), Date.now()) === 'Unknown');
  check('progress undefined → null', normalizeProgress(undefined) === null && normalizeProgress(Number.NaN) === null);
  check('progress explicit → clamped', normalizeProgress(140) === 100);

  // 10. Session status is never remapped by human gates.
  check('completed stays Completed', mapApiStatus('completed') === 'Completed');

  if (failures > 0) throw new Error(`${failures} fixture check(s) failed`);
  console.log('\nall fixture checks passed');
}

void main();
