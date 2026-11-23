export type ActivityTraceItem = {
  stage: string;
  tool?: string;
  detail?: string;
  step?: number;
};

export type RequestActivity = {
  title: string;
  detail: string;
  step?: number;
  maxSteps?: number;
  tool?: string;
  trace?: ActivityTraceItem[];
};

const stages: Record<string, RequestActivity> = {
  waiting: { title: 'Request received', detail: 'Waiting for this project’s current operation.' },
  interpreting: {
    title: 'Interpreting your request',
    detail: 'Reviewing the message and current project with the assistant.',
  },
  planning: {
    title: 'Planning engineering steps',
    detail: 'Selecting the sequence of domain tools and constraints to evaluate.',
  },
  inspecting: {
    title: 'Inspecting project state',
    detail: 'Checking current brief, labware inputs, and spatial constraints.',
  },
  researching: {
    title: 'Checking reviewed references',
    detail: 'Consulting platform specifications and source-bound component data.',
  },
  designing: {
    title: 'Updating hardware design',
    detail: 'Applying validated project edits and compiling updated geometry.',
  },
  verifying: {
    title: 'Verifying 3D fit & constraints',
    detail: 'Checking width, depth, height, access clearances, and printer envelope.',
  },
  repairing: {
    title: 'Self-repairing constraint issue',
    detail: 'Adjusting the candidate configuration from structured solver diagnostics.',
  },
  presenting: {
    title: 'Preparing final summary',
    detail: 'Formatting verified changes, assumptions, and open measurements.',
  },
  applying: {
    title: 'Applying your request',
    detail: 'Running the requested project actions and checking the result.',
  },
  sourcing: {
    title: 'Sourcing components',
    detail: 'Checking the reviewed platform catalogues for current price observations.',
  },
  assembling: {
    title: 'Assembling the 3D design',
    detail: 'Preparing the layout and checking the requested changes.',
  },
};

type ActivityPayload = {
  active: boolean;
  stage: string;
  detail?: string;
  tool?: string;
  step?: number;
  max_steps?: number;
  trace?: ActivityTraceItem[];
};

/** Poll only while the corresponding POST is pending. No invented stage sequence. */
export function watchChatActivity(
  projectId: string,
  requestId: string,
  signal: AbortSignal,
  onActivity: (activity: RequestActivity) => void,
): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const pollController = new AbortController();
  const stop = () => {
    stopped = true;
    clearTimeout(timer);
    pollController.abort();
    signal.removeEventListener('abort', stop);
  };
  const poll = async () => {
    if (stopped || signal.aborted) return;
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/activity/${encodeURIComponent(requestId)}`,
        { signal: pollController.signal, cache: 'no-store' },
      );
      if (response.ok) {
        const data = (await response.json()) as ActivityPayload;
        const base = stages[data.stage];
        if (!stopped && !signal.aborted && data.active && base) {
          onActivity({
            title: base.title,
            detail: data.detail || base.detail,
            step: data.step,
            maxSteps: data.max_steps,
            tool: data.tool,
            trace: data.trace,
          });
        }
        if (!data.active) {
          stop();
          return;
        }
      }
    } catch {
      /* The POST remains authoritative; polling failure must not fail a turn. */
    }
    if (!stopped && !signal.aborted) timer = setTimeout(() => void poll(), 650);
  };
  signal.addEventListener('abort', stop, { once: true });
  void poll();
  return stop;
}
