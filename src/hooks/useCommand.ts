import { useCallback, useRef, useState } from 'react';
import { ApiError, newIdempotencyKey, type CommandAccepted } from '../api';

export type CommandState =
  | { kind: 'idle' }
  | { kind: 'pending'; idempotencyKey: string }
  | { kind: 'accepted'; result: CommandAccepted; idempotencyKey: string }
  | { kind: 'error'; error: ApiError; idempotencyKey: string };

export interface Command<Args extends unknown[]> {
  state: CommandState;
  /** Resolves with the accepted event or null when the command failed. Never throws. */
  run: (...args: Args) => Promise<CommandAccepted | null>;
  reset: () => void;
  pending: boolean;
}

/**
 * Wraps a mutation so the UI can only claim progress after the API accepted
 * the command. Retries reuse the same idempotency key until the command
 * succeeds or is reset.
 */
export function useCommand<Args extends unknown[]>(
  execute: (idempotencyKey: string, ...args: Args) => Promise<CommandAccepted>,
  onAccepted?: (result: CommandAccepted) => void,
): Command<Args> {
  const [state, setState] = useState<CommandState>({ kind: 'idle' });
  const keyRef = useRef<string | null>(null);
  const argsRef = useRef<string | null>(null);

  const run = useCallback(
    async (...args: Args) => {
      const serializedArgs = JSON.stringify(args);
      const idempotencyKey =
        keyRef.current !== null && argsRef.current === serializedArgs
          ? keyRef.current
          : newIdempotencyKey();
      keyRef.current = idempotencyKey;
      argsRef.current = serializedArgs;
      setState({ kind: 'pending', idempotencyKey });
      try {
        const result = await execute(idempotencyKey, ...args);
        keyRef.current = null;
        argsRef.current = null;
        setState({ kind: 'accepted', result, idempotencyKey });
        onAccepted?.(result);
        return result;
      } catch (raw) {
        const error = raw instanceof ApiError ? raw : new ApiError('server_error', raw instanceof Error ? raw.message : 'Command failed');
        setState({ kind: 'error', error, idempotencyKey });
        return null;
      }
    },
    [execute, onAccepted],
  );

  const reset = useCallback(() => {
    keyRef.current = null;
    argsRef.current = null;
    setState({ kind: 'idle' });
  }, []);

  return { state, run, reset, pending: state.kind === 'pending' };
}
