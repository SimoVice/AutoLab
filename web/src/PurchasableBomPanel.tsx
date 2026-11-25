import { budgetRange } from './budgetDisplay';
import { useEffect, useRef, useState } from 'react';
import type {
  BomCurrency,
  HardwareCompatibilityReport,
  PartCandidate,
  Project,
  PurchasableBom,
  PurchasableBomLine,
} from './types';
import './purchasable-bom.css';

type ApiCapabilityStatus = {
  digikey_v4?: { configured: boolean };
  mouser_v2?: { configured: boolean };
  shopify_json?: { configured: boolean };
  frankfurter_fx?: { configured: boolean };
};

type Props = {
  project: Project;
  busy?: boolean;
  onSelectPart?: (componentId: string) => void;
};

const priceLabels: Record<PurchasableBomLine['price_source'], string> = {
  included_in_base: 'Included in base',
  unpriced: 'Selection required', unavailable: 'Supplier unavailable', stale_observation: 'Recheck supplier',
  live_api: 'Live API', slicer_material_estimate: '3D printed', curated_reference: 'Reference',
  quote_required: 'Quote required', lab_inventory: 'Lab inventory', quantity_required: 'Quantity required',
};

const evidenceLabels = { catalog: 'Reviewed catalog', supplier_search: 'Supplier search', user_record: 'User declaration' };
const reviewLabels = { matched: 'Recorded requirements match', unverified: 'Interfaces to verify', mismatch: 'Conflicts with requirement' };

function CandidateEvidence({ part }: { part: PartCandidate }) {
  return <div className="bom-line-meta">
    <strong>{part.issuer} · {part.part_number}</strong>
    <span>{evidenceLabels[part.basis]} · {reviewLabels[part.interface_status]}</span>
    {part.source_observed_at && <time className="bom-line-meta" dateTime={part.source_observed_at}>
      Identified {new Date(part.source_observed_at).toLocaleDateString('en-US', { timeZone: 'UTC' })}
    </time>}
    {part.source_url && <a href={part.source_url} target="_blank" rel="noopener noreferrer">Identity source</a>}
    <p>{part.evidence}</p>
    <ul>{part.checks.map((check, index) => <li key={index}>
      {check.key.replaceAll('_', ' ')} · {check.comparison.replaceAll('_', ' ')} {check.required} {check.unit}
      {' · '}{check.actual ?? 'Unspecified'} ({check.status})
    </li>)}</ul>
  </div>;
}

function fmtMoney(amount: number | null | undefined, currency: BomCurrency): string {
  if (amount == null || Number.isNaN(amount)) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

function fmtDuration(minutes: number): string {
  if (!minutes || minutes <= 0) return '0 min';
  const hrs = Math.floor(minutes / 60);
  const rem = Math.round(minutes % 60);
  return hrs > 0 ? `${hrs} h ${rem} min` : `${rem} min`;
}

export default function PurchasableBomPanel({ project, busy = false, onSelectPart }: Props) {
  const [currency, setCurrency] = useState<BomCurrency>('USD');
  const [bom, setBom] = useState<PurchasableBom | null>(null);
  const [compat, setCompat] = useState<HardwareCompatibilityReport | null>(null);
  const [apiCaps, setApiCaps] = useState<ApiCapabilityStatus | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const refreshRequest = useRef<AbortController | null>(null);

  const base = `/api/projects/${encodeURIComponent(project.id)}`;
  const revision = project.revision;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setBom(null);
    setCompat(null);
    setRefreshing(false);
    setError('');
    setWarnings([]);
    fetch(`${base}/bom?revision=${revision}&currency=USD`, { signal: controller.signal })
      .then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail ?? 'Could not load purchasable BOM.');
        return data as {
          bom: PurchasableBom;
          compatibility: HardwareCompatibilityReport;
          supplier_api_status: ApiCapabilityStatus;
        };
      })
      .then((data) => {
        if (!controller.signal.aborted) {
          setCurrency('USD');
          setBom(data.bom);
          setCompat(data.compatibility);
          setApiCaps(data.supplier_api_status);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : 'Failed to load purchasable BOM.');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      refreshRequest.current?.abort();
    };
  }, [base, revision]);

  async function refreshBom(targetCurrency: BomCurrency) {
    refreshRequest.current?.abort();
    const controller = new AbortController();
    refreshRequest.current = controller;
    setRefreshing(true);
    setError('');
    try {
      const res = await fetch(`${base}/bom/refresh`, {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_revision: revision,
          currency: targetCurrency,
          use_official_apis: true,
        }),
      });
      const data = await res.json();
      if (controller.signal.aborted) return;
      if (!res.ok) throw new Error(data.detail ?? 'Failed to refresh supplier prices/FX.');
      setCurrency(targetCurrency);
      setBom(data.bom);
      setCompat(data.compatibility);
      setApiCaps(data.supplier_api_status);
      setWarnings(Array.isArray(data.live_quote_warnings) ? data.live_quote_warnings : []);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'BOM refresh failed.');
    } finally {
      if (!controller.signal.aborted) setRefreshing(false);
    }
  }

  return (
    <section className="purchasable-bom-section" aria-label="Purchasing list and hardware compatibility">
      <div className="bom-header">
        <div>
          <h3>Purchasing list &amp; hardware compatibility</h3>
          <p>Catalog parts, fabrication estimates and open requirements for revision {revision}.</p>
        </div>
        <div className="bom-toolbar">
          <label>
            <span className="sr-only">Currency</span>
            <select
              aria-label="BOM currency"
              value={currency}
              disabled={busy || loading || refreshing || bom?.status === 'design_required'}
              onChange={(event) => void refreshBom(event.target.value as BomCurrency)}
            >
              <option value="USD">USD ($)</option>
              <option value="CHF">CHF (Fr.)</option>
              <option value="EUR">EUR (€)</option>
            </select>
          </label>
          <button
            type="button"
            className="bom-refresh-btn"
            disabled={busy || loading || refreshing || bom?.status === 'design_required'}
            onClick={() => void refreshBom(currency)}
          >
            {refreshing ? 'Refreshing APIs & FX…' : 'Refresh live API prices'}
          </button>
          <a
            href={`${base}/bom/download?revision=${revision}&format=csv&currency=${currency}`}
            download
          >
            BOM CSV
          </a>
          <a
            href={`${base}/bom/download?revision=${revision}&format=json&currency=${currency}`}
            download
          >
            BOM JSON
          </a>
          {bom && bom.route_kind !== 'unselected' && (
            <a href={`${base}/physical-assembly.json?revision=${revision}`} download>
              Assembly JSON
            </a>
          )}
        </div>
      </div>

      {error && (
        <p className="resource-error" role="alert">
          {error}
        </p>
      )}
      {loading && <p role="status">Computing itemized BOM and compatibility checks…</p>}

      {compat && compat.status !== 'not_evaluated' && (
        <div className={`compat-banner compat-${compat.status}`} role="region" aria-label="Hardware compatibility">
          <div className="compat-banner-top">
            <strong>{compat.summary}</strong>
            <span className="compat-status-pill">{compat.status}</span>
          </div>
          <details open={compat.error_count > 0 || compat.warning_count > 0}>
            <summary>View {compat.checks.length} compatibility checks</summary>
            <ul className="compat-list">
              {compat.checks.map((chk) => (
                <li key={chk.id} className={`compat-item sev-${chk.severity}`}>
                  <strong>
                    [{chk.severity.toUpperCase()}] {chk.title}
                  </strong>{' '}
                  — {chk.detail}
                  {chk.recommended_fix && (
                    <span className="compat-fix">Fix: {chk.recommended_fix}</span>
                  )}
                </li>
              ))}
            </ul>
            <p className="bom-exclusions">{compat.scope_disclaimer}</p>
          </details>
        </div>
      )}

      {bom?.status === 'design_required' && (
        <p role="status">Choose or develop a hardware design in chat to see its parts and costs.</p>
      )}
      {bom && bom.status !== 'design_required' && (
        <>
          <div className="bom-kpis">
            <div className="bom-kpi-card">
              <span>Documented prices subtotal ({bom.currency})</span>
              <strong>{fmtMoney(bom.summary.documented_subtotal, bom.currency)}</strong>
              <small>
                {bom.summary.line_count} lines · {bom.summary.unresolved_count} awaiting selection
                {bom.summary.unavailable_count > 0 && ` · ${bom.summary.unavailable_count} unavailable`}
                {bom.summary.quote_required_count > 0 && ` · ${bom.summary.quote_required_count} awaiting a quote`}
                {bom.summary.stale_offer_count > 0 && ` · ${bom.summary.stale_offer_count} awaiting a new price`}
              </small>
            </div>
            <div className="bom-kpi-card">
              <span>Current supplier prices</span>
              <strong>{fmtMoney(bom.summary.live_api_priced_count ? bom.summary.live_api_subtotal : null, bom.currency)}</strong>
              <small>{bom.summary.live_api_priced_count} live API line(s)</small>
            </div>
            <div className="bom-kpi-card">
              <span>Catalog references</span>
              <strong>{fmtMoney(bom.summary.reference_priced_count ? bom.summary.reference_catalog_subtotal : null, bom.currency)}</strong>
              <small>
                Checked {bom.catalog_checked_on}
                {bom.fx_rate_used ? ` · FX ${bom.fx_rate_used}` : ''}
              </small>
            </div>
            <div className="bom-kpi-card">
              <span>Filament estimate</span>
              <strong>{fmtMoney(bom.summary.printed_line_count ? bom.summary.printed_material_subtotal : null, bom.currency)}</strong>
              <small>
                {bom.summary.total_filament_mass_g.toFixed(0)} g ·{' '}
                {fmtDuration(bom.summary.total_print_time_minutes)}
              </small>
            </div>
            <div className="bom-kpi-card">
              <span>Planning allowances</span>
              <strong>{budgetRange(bom.summary.planning_low, bom.summary.planning_high, bom.currency)}</strong>
              <small>{bom.summary.missing_price_count} lines still need purchase prices</small>
            </div>
            <div className="bom-kpi-card">
              <span>Longest known lead time</span>
              <strong>
                {bom.summary.max_lead_time_days > 0 ? `${bom.summary.max_lead_time_days} d` : '—'}
              </strong>
              <small>Excludes shipping &amp; customs</small>
            </div>
          </div>

          <div className="bom-table-scroll">
            <table className="bom-table">
              <thead>
                <tr>
                  <th>Part / MPN</th>
                  <th>Source</th>
                  <th>Qty (Packs)</th>
                  <th>Line Total</th>
                  <th>Stock / Lead</th>
                </tr>
              </thead>
              <tbody>
                {bom.lines.map((line) => (
                  <tr key={line.line_id}>
                    <td>
                      <strong>{line.label}</strong>
                      {line.part_choice?.accepted && <span className="bom-line-meta">
                        {line.requirement?.name} · {reviewLabels[line.part_choice.accepted.interface_status]}
                      </span>}
                      {!line.part_choice?.accepted && !!line.requirement?.critical_specs?.length && (
                        <details className="bom-choice">
                          <summary>Required specifications</summary>
                          <ul>{line.requirement.critical_specs.map((spec, index) => <li key={index}>
                            {spec.key.replaceAll('_', ' ')} · {spec.comparison.replaceAll('_', ' ')} {spec.value} {spec.unit}
                            {spec.evidence && <span className="bom-line-meta">{spec.evidence}</span>}
                          </li>)}</ul>
                        </details>
                      )}
                      {line.part_choice && line.part_choice.candidates.length > 0 && <details className="bom-choice">
                        <summary>Part evidence ({line.part_choice.candidates.length})</summary>
                        {line.part_choice.decision && <p>Selection · {line.part_choice.decision}</p>}
                        {line.part_choice.candidates.map(part => <CandidateEvidence
                          key={JSON.stringify([part.issuer, part.part_number, part.label])} part={part} />)}
                      </details>}
                      {line.requirement?.source_file && (
                        <span className="bom-line-meta">
                          Source list · {[line.requirement.source_file, line.requirement.supplier,
                            line.requirement.mpn ?? line.requirement.sku ?? line.requirement.part_number]
                            .filter(Boolean).join(' · ')}
                        </span>
                      )}
                      <span className="bom-line-meta">
                        {line.supplier}
                        {line.mpn && line.kind === 'commercial_part' ? ` · MPN ${line.mpn}` : ''}
                        {line.notes && `${line.supplier || line.mpn ? ' · ' : ''}${line.notes}`}
                      </span>
                      {line.print_estimate && (
                        <span className="bom-line-meta">
                          {line.print_estimate.material} ({line.print_estimate.bounding_box_mm.join('×')} mm) ·{' '}
                          {line.print_estimate.filament_mass_g.toFixed(1)} g ·{' '}
                          {fmtDuration(line.print_estimate.print_time_minutes)}
                        </span>
                      )}
                      {onSelectPart && line.component_ids.length > 0 && (
                        <button
                          type="button"
                          className="bom-select-3d"
                          onClick={() => onSelectPart(line.component_ids[0])}
                        >
                          Show in 3D
                        </button>
                      )}
                    </td>
                    <td>
                      <span className={`bom-badge bom-badge-${line.price_source}`}>
                        {priceLabels[line.price_source]}
                      </span>
                      {line.fetched_at && <time className="bom-line-meta" dateTime={line.fetched_at}>
                        Checked {new Date(line.fetched_at).toLocaleDateString('en-US', { timeZone: 'UTC' })}
                      </time>}
                      {line.source_url && (
                        <a
                          href={line.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="bom-line-meta"
                        >
                          {line.part_choice?.accepted?.basis === 'user_record' ? 'Recorded source' : 'Supplier link'}
                        </a>
                      )}
                    </td>
                    <td>
                      {line.quantity} {line.unit}
                      {line.purchase_quantity != null && <span className="bom-line-meta">
                        {line.owned_quantity} on hand · {line.purchase_quantity} to purchase
                      </span>}
                      {line.packs_to_order != null && line.pack_size != null && line.pack_size > 1 && (
                        <span className="bom-line-meta">
                          {line.packs_to_order} pack(s) × {line.pack_size}
                        </span>
                      )}
                    </td>
                    <td>
                      <strong>{fmtMoney(line.line_total, bom.currency)}</strong>
                      {line.planning_basis && <span className="bom-line-meta">Planning allowance · {budgetRange(line.planning_low, line.planning_high, bom.currency)}<br />{line.planning_basis.detail}
                        {line.planning_basis.source_url && <a href={line.planning_basis.source_url} target="_blank" rel="noreferrer">Source · {line.planning_basis.observed_on}</a>}
                      </span>}
                      {line.unit_price != null && <span className="bom-line-meta">
                        {fmtMoney(line.unit_price, bom.currency)} / {line.unit}
                      </span>}
                    </td>
                    <td>
                      <span>
                        {line.stock_quantity != null
                          ? `${line.stock_quantity} in stock`
                          : line.resolution_status === 'selection_required' ? '—'
                            : line.availability.replaceAll('_', ' ')}
                      </span>
                      {line.lead_time_days != null && (
                        <span className="bom-line-meta">~{line.lead_time_days} d lead</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="bom-exclusions">
            {bom.shipping_and_tax_scope}{' '}
            {bom.fx_source ? `FX: ${bom.fx_source} (${bom.fx_rate_date}). ` : ''}
            {apiCaps && (
              <>
                APIs: DigiKey ({apiCaps.digikey_v4?.configured ? 'ready' : 'key unset'}), Mouser (
                {apiCaps.mouser_v2?.configured ? 'ready' : 'key unset'}), Shopify JSON (ready).
              </>
            )}
          </p>
          {warnings.length > 0 && (
            <details className="bom-exclusions">
              <summary>Live quote fallback notes ({warnings.length})</summary>
              <ul>
                {warnings.map((w, idx) => (
                  <li key={idx}>{w}</li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  );
}
