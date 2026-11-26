import type { OperationSequence } from './types';
import RecordingReplay from './RecordingReplay';

type Props = { sequence: OperationSequence; href: string; softwareUrl: string };

export default function OperationSequenceView({ sequence, href, softwareUrl }: Props) {
  if (!sequence.operations.length) return <p>Define the operations and choose a design in chat.</p>;
  const location = (role?: string | number, well?: string | number) =>
    role && well ? `${String(role).replaceAll('_', ' ')} / ${well}` : '';
  return <section className="bound-sequence" aria-label="Device operation plan">
    <h3>Device operation plan</h3>
    <p>{sequence.operations.length} operations · revision {sequence.revision} · {sequence.scope === 'approved' ? 'Approved design' : 'Illustrative configuration'}</p>
    {sequence.hardware_blockers.map(reason => <p key={reason} className="sequence-blocker">{reason}</p>)}
    <ol aria-label="Ordered device operations">{sequence.operations.map(op => {
      const part = sequence.bindings[op.binding];
      const p = op.parameters;
      const places = [location(p.source_role, p.source_well), location(p.dest_role || p.role, p.dest_well || p.well)].filter(Boolean);
      return <li key={op.id}>
        <strong>{op.action.replaceAll('_', ' ')}</strong>
        {p.volume_ul != null && <> · {p.volume_ul} µL</>}
        {p.repetitions != null && <> × {p.repetitions}</>}
        {p.new_tip === 'always' && <> · fresh tip</>}
        {places.length > 0 && <span>{places.join(' → ')}</span>}
        <small>{op.id} · {op.mode === 'driver' ? part.driver : op.mode === 'manual' ? 'Operator step' : op.mode === 'recorded_input' ? 'Recorded input' : 'Missing driver'}</small>
        {part.adapter?.missing_capability && <span>Unsupported command · {part.adapter.missing_capability}</span>}
        {part.adapter?.integration_requirements.map(need => <span key={need}>{need}</span>)}
      </li>;
    })}</ol>
    <details><summary>Device bindings and prerequisites</summary>
      {Object.entries(sequence.bindings).map(([key, b]) =>
        <div key={key}><strong>{b.capability.replaceAll('_', ' ')}</strong>
          <p>{b.component_id || 'Component unassigned'}{b.unit && ` · ${b.unit}`}</p>
          {Object.entries(b.limits).map(([name, value]) => <p key={name}>{name.replaceAll('_', ' ')} · {value == null ? 'Unverified' : String(value)}</p>)}
          {b.adapter && <p>{b.adapter.id} · {b.adapter.command} · recorded input only</p>}
          {b.prerequisites.map(v => <p key={v}>{v}</p>)}</div>)}
    </details>
    <a href={href} download="operation-plan.json">Download operation plan</a>
    {Object.values(sequence.bindings).some(b => b.adapter) &&
      <RecordingReplay key={sequence.sha256} softwareUrl={softwareUrl} revision={sequence.revision} />}
  </section>;
}
