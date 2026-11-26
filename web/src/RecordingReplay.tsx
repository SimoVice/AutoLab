import { useEffect, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';

type ReplayResult = {
  status: 'blocked' | 'replayed' | 'stopped';
  item_id: string;
  events: {
    operation_id: string;
    action: string;
    value?: number | string;
    unit?: string;
    note?: string;
    outcome?: string;
    calibration_reference?: string;
    outside_reference_span?: boolean;
  }[];
  issues: {
    operation_id?: string;
    reason?: string;
    missing_capability?: string;
    integration_requirements?: string[];
  }[];
};

export default function RecordingReplay({ softwareUrl, revision }: { softwareUrl: string; revision: number }) {
  const [result, setResult] = useState<ReplayResult | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const query = `?revision=${revision}`;
  useEffect(() => () => pending.current?.abort(), []);

  async function replay(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setResult(null);
    setError('');
    setBusy(true);
    try {
      if (file.size > 8 * 1024 * 1024) throw new Error('Choose a recording smaller than 8 MiB.');
      const response = await fetch(`${softwareUrl}/replay${query}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: file, signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Cannot read the recording.');
      if (!controller.signal.aborted) setResult(data);
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Replay failed.');
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <section className="recording-replay" aria-label="Recording replay">
    <h3>Replay a recording</h3>
    <p>Fill the template with instrument inputs and operator notes from this revision.</p>
    <div className="replay-downloads">
      <a href={`${softwareUrl}/recording-template${query}`} download="recording-template.json">Download template</a>
      <a href={`${softwareUrl}/replay-kit.zip${query}`} download>Offline replay kit</a>
    </div>
    <label>Recording file (JSON)<input type="file" accept=".json,application/json" onClick={e => { e.currentTarget.value = ''; }} onChange={replay} /></label>
    {busy && <p role="status">Reading recorded inputs…</p>}
    {error && <p role="alert">{error}</p>}
    {result && <div role="status">
      <strong>{result.status === 'blocked' ? 'Replay blocked' : result.status === 'stopped' ? 'Replay stopped at a failed check' : `Recorded results · ${result.item_id}`}</strong>
      {result.issues.map((issue, i) => <p key={issue.operation_id ?? i}>
        {[issue.operation_id, issue.missing_capability, issue.reason, ...(issue.integration_requirements ?? [])].filter(Boolean).join(' · ')}
      </p>)}
      {result.events.length > 0 && <ul>{result.events.map(ev => <li key={ev.operation_id}>
        {ev.action} · {ev.outcome && `${ev.outcome} · `}{ev.note ?? ev.value}{!ev.note && ev.unit && ev.unit !== 'identifier' && ` ${ev.unit}`}
        {ev.calibration_reference && <small>Calibration reference · {ev.calibration_reference}</small>}
        {ev.outside_reference_span && <strong>Outside the reference span</strong>}
      </li>)}</ul>}
    </div>}
  </section>;
}
