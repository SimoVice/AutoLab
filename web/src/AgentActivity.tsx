import type { RequestActivity } from './chatActivity';

export type AgentPhase = 'pending' | 'updating' | 'ready';

export const activityCopy = {
  pending: { title: 'Reviewing your request', detail: 'The assistant is checking your request.' },
  updating: { title: 'Updating design', detail: 'Applying the accepted changes to the model.' },
  ready: { title: 'Design updated', detail: 'Review the accepted changes in the model.' },
} satisfies Record<AgentPhase, { title: string; detail: string }>;

/** Yield to the browser paint; no estimated work percentage or artificial progress delay. */
export function nextDesignPaint(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) { resolve(); return; }
    let frame = 0;
    const finish = () => { cancelAnimationFrame(frame); signal.removeEventListener('abort', finish); resolve(); };
    signal.addEventListener('abort', finish, { once: true });
    frame = requestAnimationFrame(() => { frame = requestAnimationFrame(finish); });
  });
}

export default function AgentActivity({
  phase,
  pendingTitle,
  activity,
  scene = false,
}: {
  phase: AgentPhase;
  pendingTitle?: string | null;
  activity?: RequestActivity | null;
  scene?: boolean;
}) {
  const copy =
    phase === 'pending' && activity
      ? activity
      : phase === 'pending' && pendingTitle
        ? { title: pendingTitle, detail: 'Checking the selected product and placement.' }
        : activityCopy[phase];
  const stepLabel =
    phase === 'pending' && activity?.step != null && activity?.maxSteps != null
      ? `Step ${activity.step}/${activity.maxSteps}`
      : null;
  const recentTrace =
    phase === 'pending' && !scene && activity?.trace?.length
      ? activity.trace.slice(-4)
      : [];

  return (
    <div
      className={scene ? `design-activity design-activity-${phase}` : 'agent-activity'}
      aria-hidden={scene || undefined}
      role={scene ? undefined : 'status'}
      aria-live={scene ? undefined : 'polite'}
      aria-atomic={scene ? undefined : true}
      data-phase={phase}
    >
      <span className={`activity-indicator${phase === 'ready' ? ' activity-ready' : ''}`}>
        {phase === 'ready' ? (
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <path d="m5 12 4 4L19 6" />
          </svg>
        ) : (
          <span />
        )}
      </span>
      <div className="agent-activity-heading">
        <div className="agent-activity-title-row">
          <strong>{copy.title}</strong>
          {stepLabel && <span className="activity-step-badge">{stepLabel}</span>}
        </div>
        <span>{copy.detail}</span>
        {recentTrace.length > 1 && (
          <ul className="activity-trace-list" aria-label="Autonomous tool steps">
            {recentTrace.map((item, idx) => (
              <li key={idx}>
                {item.tool ? <code>{item.tool}</code> : item.stage}
                {item.detail ? ` — ${item.detail}` : ''}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
