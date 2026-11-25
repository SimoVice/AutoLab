import type { DesignPlan } from './types';

export default function DesignRoutes({ plan, needsReview }: { plan: DesignPlan; needsReview: boolean }) {
  const strategies = { reuse: 'Reuse', buy: 'Purchase', fabricate: 'Fabricate' };
  const selected = plan.options.find(option => option.key === plan.decision?.key);
  const status = needsReview ? 'Requirements changed' : selected?.label;
  return <details className="cad-instance-details design-routes">
    <summary>Design options{status && <> · {status}</>}</summary>
    {needsReview && <p role="status">Review these options against the changed brief before applying a design.</p>}
    {plan.pending_workspace && <p><strong>Requested workspace</strong> · {[
      ['width', plan.pending_workspace.workspace_width_mm], ['depth', plan.pending_workspace.workspace_depth_mm],
      ['height', plan.pending_workspace.workspace_height_mm],
    ].filter(([, value]) => value != null).map(([axis, value]) => `${axis} ${value} mm`).join(', ')}</p>}
    <section aria-label="Required operations">
      <h3>Required operations</h3>
      <ol>{plan.operations.map(op => <li key={op.key}>
        <strong>{op.action}</strong>{[op.material, op.performance, op.interface].filter(Boolean).map((value, i) =>
          <span key={i}> · {value}</span>)}
      </li>)}</ol>
    </section>
    {plan.options.map(option => {
      const chosen = option.key === selected?.key;
      const application = plan.decision?.applied_revision;
      return <section key={option.key} aria-label={option.label}>
        <h3>{option.label} <small>{strategies[option.strategy]}</small></h3>
        {chosen && <p><strong>{application != null ? `Applied in revision ${application}` : 'Selected for design'}</strong></p>}
        <p>{option.benefit}</p><p>{option.tradeoff}</p>
        <details className="route-coverage">
          <summary>Operation coverage</summary>
          <ul>{option.coverage.map(c => <li key={c.operation}>
            {plan.operations.find(op => op.key === c.operation)?.action} · {c.capability.replaceAll('_', ' ')}
          </li>)}</ul>
        </details>
        {option.inventory_quote && <p>Available equipment · {option.inventory_quote}</p>}
        {option.measurements.map(m => <p key={m.key}>Measured · {m.quote}</p>)}
        {plan.blockers[option.key]?.map(reason => <p key={reason}><strong>Open</strong> · {reason}</p>)}
        {plan.sources.filter(s => option.source_ids.includes(s.key)).map(s =>
          <p key={s.key}>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.label}</a> : s.label} · {s.detail}</p>)}
      </section>;
    })}
  </details>;
}
