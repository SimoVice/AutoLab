import { useEffect, useState } from 'react';
import type {
  BomCurrency,
  HardwareCompatibilityReport,
  Project,
  PurchasableBom,
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

  const base = `/api/projects/${encodeURIComponent(project.id)}`;
  const revision = project.revision;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
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
    return () => controller.abort();
  }, [base, revision]);

  async function refreshBom(targetCurrency: BomCurrency) {
    setRefreshing(true);
    setError('');
    try {
      const res = await fetch(`${base}/bom/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_revision: revision,
          currency: targetCurrency,
          use_official_apis: true,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Failed to refresh supplier prices/FX.');
      setCurrency(targetCurrency);
      setBom(data.bom);
      setCompat(data.compatibility);
      setApiCaps(data.supplier_api_status);
      setWarnings(Array.isArray(data.live_quote_warnings) ? data.live_quote_warnings : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'BOM refresh failed.');
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <section className="purchasable-bom-section" aria-label="Complete purchasable BOM and compatibility">
      <div className="bom-header">
        <div>
          <h3>Purchasable BOM &amp; Hardware Compatibility</h3>
          <p>
            Itemized commercial MPNs, pack rounding, analytical 3D print estimates, and
            deterministic electrical/mechanical checks (rev {revision}).
          </p>
        </div>
        <div className="bom-toolbar">
          <label>
            <span className="sr-only">Currency</span>
            <select
              aria-label="BOM currency"
              value={currency}
              disabled={busy || loading || refreshing}
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
            disabled={busy || loading || refreshing}
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
        </div>
      </div>

      {error && (
        <p className="resource-error" role="alert">
          {error}
        </p>
      )}
      {loading && <p role="status">Computing itemized BOM and compatibility checks…</p>}

      {compat && (
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

      {bom && (
        <>
          <div className="bom-kpis">
            <div className="bom-kpi-card">
              <span>Estimated Total ({bom.currency})</span>
              <strong>{fmtMoney(bom.summary.total_estimated_cost, bom.currency)}</strong>
              <small>
                {bom.summary.line_count} lines ({bom.summary.unpriced_placeholder_count} placeholders)
              </small>
            </div>
            <div className="bom-kpi-card">
              <span>Live API Verified</span>
              <strong>{fmtMoney(bom.summary.live_api_subtotal, bom.currency)}</strong>
              <small>{bom.summary.live_api_priced_count} live API line(s)</small>
            </div>
            <div className="bom-kpi-card">
              <span>Curated Catalog Ref.</span>
              <strong>{fmtMoney(bom.summary.reference_catalog_subtotal, bom.currency)}</strong>
              <small>
                Checked {bom.catalog_checked_on}
                {bom.fx_rate_used ? ` · FX ${bom.fx_rate_used}` : ''}
              </small>
            </div>
            <div className="bom-kpi-card">
              <span>3D Printed Parts</span>
              <strong>{fmtMoney(bom.summary.printed_material_subtotal, bom.currency)}</strong>
              <small>
                {bom.summary.total_filament_mass_g.toFixed(0)} g ·{' '}
                {fmtDuration(bom.summary.total_print_time_minutes)}
              </small>
            </div>
            <div className="bom-kpi-card">
              <span>Max Lead Time</span>
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
                      <span className="bom-line-meta">
                        {line.supplier ?? 'In-house'}
                        {line.mpn ? ` · MPN ${line.mpn}` : ''}
                        {line.notes ? ` · ${line.notes}` : ''}
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
                        {line.price_source === 'live_api'
                          ? 'Live API'
                          : line.price_source === 'slicer_material_estimate' ||
                              line.kind === '3d_printed'
                            ? '3D Printed'
                            : 'Reference'}
                      </span>
                      {line.source_url && (
                        <a
                          href={line.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="bom-line-meta"
                        >
                          Supplier link
                        </a>
                      )}
                    </td>
                    <td>
                      {line.quantity} {line.unit}
                      {line.pack_size > 1 && (
                        <span className="bom-line-meta">
                          {line.packs_to_order} pack(s) × {line.pack_size}
                        </span>
                      )}
                    </td>
                    <td>
                      <strong>{fmtMoney(line.line_total, bom.currency)}</strong>
                      <span className="bom-line-meta">
                        {fmtMoney(line.unit_price, bom.currency)} / {line.unit}
                      </span>
                    </td>
                    <td>
                      <span>
                        {line.stock_quantity != null
                          ? `${line.stock_quantity} in stock`
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
