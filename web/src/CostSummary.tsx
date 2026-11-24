import { useEffect, useState } from 'react';
import { budgetRange } from './budgetDisplay';
import type { ProcurementReport, Project, PurchasableBom } from './types';

type Estimate = {
  project_id: string; project_revision: number; expires_at: string | null;
  summary: PurchasableBom['summary'];
};

export type CostEstimateState = { loading: boolean; issue: string | null; updated: boolean };

export default function CostSummary({ project, report, headless = false, refreshToken = 0, onState }: {
  project: Project; report: ProcurementReport | null; headless?: boolean; refreshToken?: number;
  onState?: (state: CostEstimateState) => void;
}) {
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [issue, setIssue] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const hasDesign = Boolean(project.engineering_design || project.bench_station || project.concept_design ||
    project.standalone_fixture || project.upstream_project);
  useEffect(() => {
    if (!hasDesign) return;
    const controller = new AbortController();
    setEstimate(null);
    setIssue(null);
    onState?.({ loading: true, issue: null, updated: false });
    fetch(`/api/projects/${encodeURIComponent(project.id)}/cost-estimate?revision=${project.revision}`, { signal: controller.signal })
      .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail ?? 'Could not load the purchasing breakdown.');
        if (!controller.signal.aborted) {
          setEstimate(data.estimate);
          onState?.({ loading: false, issue: null, updated: true });
        }
      })
      .catch(error => {
        if (!controller.signal.aborted) {
          setIssue(error.message);
          onState?.({ loading: false, issue: error.message, updated: false });
        }
      });
    return () => controller.abort();
  }, [project.id, project.revision, hasDesign, report?.report_id, refreshToken, attempt, onState]);
  useEffect(() => {
    if (!estimate?.expires_at) return;
    const timer = window.setTimeout(() => setAttempt(n => n + 1), Math.max(0, Date.parse(estimate.expires_at) - Date.now() + 20));
    return () => window.clearTimeout(timer);
  }, [estimate]);
  if (headless) return null;
  const s = estimate?.project_id === project.id && estimate.project_revision === project.revision ? estimate.summary : null;
  return <section className="cost-block cost-summary" aria-label="Project cost estimate">
    <span>Documented prices subtotal · USD</span>
    <strong>{s ? s.documented_subtotal == null ? 'Price needed' : budgetRange(s.documented_subtotal, s.documented_subtotal)
      : issue ? 'Unavailable' : hasDesign ? 'Loading prices…' : 'Select a design'}</strong>
    {s && <dl className="cost-breakdown">
      <div><dt>Planning allowances</dt><dd>{budgetRange(s.planning_low, s.planning_high)}</dd></div>
      <div><dt>Filament estimate</dt><dd>{s.printed_line_count ? budgetRange(s.printed_material_subtotal, s.printed_material_subtotal) : 'No printed parts'}</dd></div>
      <div><dt>Missing purchase prices</dt><dd>{s.missing_price_count}</dd></div>
    </dl>}
    {issue && <small role="status">{issue} <button onClick={() => setAttempt(n => n + 1)}>Retry</button></small>}
  </section>;
}
