import { AttachedCadModels } from './AttachedCad';
import { Bounds, OrbitControls, useBounds } from '@react-three/drei';
import { Canvas, useThree } from '@react-three/fiber';
import { useEffect, useMemo, useRef, useState } from 'react';
import { OrthographicCamera } from 'three';
import GeometryPart from './GeometryPart';
import PurchasableBomPanel from './PurchasableBomPanel';
import type { BuildLogEntry, Project, StandaloneFixture } from './types';
import './fixture-panel.css';

const SCALE = 100;

function FitOnResize({ revision, turn }: { revision: number; turn: number }) {
  const bounds = useBounds();
  const size = useThree((state) => state.size);
  useEffect(() => {
    bounds.refresh().clip().fit();
  }, [bounds, size.width, size.height, revision, turn]);
  return null;
}

function KeyboardZoom({ zoom }: { zoom: number }) {
  const camera = useThree((state) => state.camera);
  const invalidate = useThree((state) => state.invalidate);
  const previous = useRef(zoom);
  useEffect(() => {
    const delta = zoom - previous.current;
    previous.current = zoom;
    if (delta && camera instanceof OrthographicCamera) {
      camera.zoom = Math.max(25, Math.min(1200, camera.zoom * Math.pow(1.2, delta)));
      camera.updateProjectionMatrix();
      invalidate();
    }
  }, [zoom, camera, invalidate]);
  return null;
}

export default function FixturePanel({
  project,
  fixture,
  panel,
  busy = false,
  selectedId = null,
  onSelect,
  onProject,
}: {
  project: Project;
  fixture: StandaloneFixture;
  panel: 'design' | 'parts' | 'instructions';
  busy?: boolean;
  selectedId?: string | null;
  onSelect: (id: string) => void;
  onProject: (next: Project, assistantMessage?: string) => void;
}) {
  const [turn, setTurn] = useState(0);
  const [zoom, setZoom] = useState(0);
  const [viewKey, setViewKey] = useState(0);
  const [selectedParts, setSelectedParts] = useState<string[]>(() =>
    fixture.parts.map((part) => part.id),
  );
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [fitResult, setFitResult] = useState<BuildLogEntry['fit_result']>('too_tight');
  const [measuredMm, setMeasuredMm] = useState<string>('');
  const [notes, setNotes] = useState<string>('');

  const reprintIds = useMemo(
    () => (project.build_feedback?.parts_to_reprint ?? []).map((item) => item.id),
    [project.build_feedback],
  );
  const unchangedIds = useMemo(
    () => new Set((project.build_feedback?.parts_unchanged ?? []).map((item) => item.id)),
    [project.build_feedback],
  );
  const reprintSet = useMemo(() => new Set(reprintIds), [reprintIds]);

  useEffect(() => {
    const allIds = fixture.parts.map((part) => part.id);
    setSelectedParts(reprintIds.length > 0 ? reprintIds : allIds);
  }, [project.revision, fixture.parts, reprintIds]);

  const revision = project.revision;
  const downloadBase = `/api/projects/${encodeURIComponent(project.id)}/fixture/download`;
  const partsQuery = selectedParts.length > 0 ? `&parts=${encodeURIComponent(selectedParts.join(','))}` : '';
  const packageUrl = `${downloadBase}/package.zip?revision=${revision}${partsQuery}`;
  const isUserMeasured = fixture.spec.dimension_provenance === 'user_measured';

  function togglePart(id: string) {
    setSelectedParts((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  async function submitBuildLog() {
    if (submitting || busy) return;
    setSubmitting(true);
    setFormError(null);
    try {
      const measuredVal = measuredMm.trim() ? Number(measuredMm.trim()) : null;
      if (measuredMm.trim() && (!Number.isFinite(measuredVal) || (measuredVal ?? 0) <= 0)) {
        throw new Error('Measured caliper value must be a positive number in mm.');
      }
      const payload: Record<string, unknown> = {
        parts_printed: selectedParts.length > 0 ? selectedParts : ['locating-base'],
        printer_name: fixture.parts[0]?.printer_fit.printer_name ?? 'Bench FDM printer',
        material: fixture.spec.material,
        nozzle_mm: fixture.spec.nozzle_mm,
        layer_height_mm: fixture.spec.layer_height_mm,
        fit_result: fitResult,
        notes: notes.trim(),
      };
      if (measuredVal !== null) {
        payload.measured_features = [
          {
            feature: 'pocket_length',
            axis: 'length',
            nominal_mm: fixture.derived.pocket_length_mm,
            measured_mm: measuredVal,
          },
        ];
      }
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/build-logs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_revision: revision, log: payload }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.project) {
        throw new Error(typeof data?.detail === 'string' ? data.detail : 'Build log could not be saved.');
      }
      onProject(data.project as Project, `Recorded physical build log ${data.log.id} (${data.log.fit_result}).`);
      setMeasuredMm('');
      setNotes('');
    } catch (reason: unknown) {
      setFormError(reason instanceof Error ? reason.message : 'Failed to save build log.');
    } finally {
      setSubmitting(false);
    }
  }

  async function applyFeedbackRevision(logId: string) {
    if (submitting || busy) return;
    setSubmitting(true);
    setFormError(null);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(project.id)}/build-logs/${encodeURIComponent(logId)}/revise`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_revision: revision }),
        },
      );
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.project) {
        throw new Error(typeof data?.detail === 'string' ? data.detail : 'Could not apply tolerance revision.');
      }
      onProject(data.project as Project, data.assistant_message as string | undefined);
    } catch (reason: unknown) {
      setFormError(reason instanceof Error ? reason.message : 'Failed to apply tolerance revision.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className={`fixture-panel fixture-panel-${panel}`} aria-label="Standalone printable fixture">
      <div className="fixture-stage" hidden={panel !== 'design'}>
        <div className="fixture-heading">
          <div>
            <h2>{fixture.spec.title}</h2>
            <p>
              {fixture.spec.object_label} · revision {revision}
            </p>
          </div>
          <span className={`provenance-badge ${isUserMeasured ? 'measured' : 'reference'}`}>
            {isUserMeasured ? 'User measured' : 'Nominal reference'}
          </span>
        </div>

        <div className="fixture-view">
          <Canvas
            key={viewKey}
            aria-label="Interactive standalone fixture 3D preview"
            orthographic
            frameloop="demand"
            camera={{ position: [1.8, 1.6, 2.2], zoom: 180, near: 0.01, far: 100 }}
          >
            <color attach="background" args={['#edf3f1']} />
            <ambientLight intensity={1.5} />
            <directionalLight position={[2, 4, 3]} intensity={2.4} />
            <directionalLight position={[-3, 2, -2]} intensity={1.1} />
            <Bounds fit clip observe margin={1.15} maxDuration={0}>
              <group rotation={[0, (turn * Math.PI) / 4, 0]}>
                <AttachedCadModels project={project} scale={1 / SCALE} />
                {fixture.components.map((comp) => (
                  <GeometryPart
                    key={comp.id}
                    part={comp}
                    position={[
                      comp.position[0] / SCALE,
                      comp.position[1] / SCALE,
                      comp.position[2] / SCALE,
                    ]}
                    selected={selectedId === comp.id}
                    onSelect={onSelect}
                  />
                ))}
              </group>
              <FitOnResize revision={revision} turn={turn} />
            </Bounds>
            <KeyboardZoom zoom={zoom} />
            <OrbitControls makeDefault enablePan={false} minZoom={30} maxZoom={1200} maxPolarAngle={Math.PI / 2 - 0.03} />
          </Canvas>
          <div className="bench-station-controls" aria-label="Fixture 3D controls">
            <button type="button" onClick={() => setTurn((v) => v - 1)} aria-label="Rotate fixture left">↶</button>
            <button type="button" onClick={() => setTurn((v) => v + 1)} aria-label="Rotate fixture right">↷</button>
            <button type="button" onClick={() => setZoom((v) => v + 1)} aria-label="Zoom in">+</button>
            <button type="button" onClick={() => setZoom((v) => v - 1)} aria-label="Zoom out">−</button>
            <button type="button" onClick={() => { setTurn(0); setZoom(0); setViewKey((v) => v + 1); }}>Reset view</button>
          </div>
          <span className="bench-station-hint">
            {fixture.components.find((c) => c.id === selectedId)?.label ?? 'Drag to rotate · Scroll to zoom'}
          </span>
        </div>

        <div className="fixture-meta">
          <div>
            <span>Envelope (W × D × H)</span>
            <strong>
              {fixture.footprint_mm[0].toFixed(1)} × {fixture.footprint_mm[1].toFixed(1)} × {fixture.height_mm.toFixed(1)} mm
            </strong>
          </div>
          <div>
            <span>Pocket allowance</span>
            <strong>
              {fixture.derived.pocket_length_mm.toFixed(2)} × {fixture.derived.pocket_width_mm.toFixed(2)} mm
            </strong>
          </div>
        </div>
      </div>

      <div className="fixture-downloads-card">
        <div className="fixture-downloads-header">
          <h3>Print-ready CAD, drawings &amp; selective export</h3>
          <div className="fixture-select-actions">
            <button
              type="button"
              onClick={() => setSelectedParts(fixture.parts.map((p) => p.id))}
            >
              Select all
            </button>
            {reprintIds.length > 0 && (
              <button type="button" onClick={() => setSelectedParts(reprintIds)}>
                Select reprint only ({reprintIds.length})
              </button>
            )}
          </div>
        </div>

        <ul className="fixture-parts-list">
          {fixture.parts.map((part) => {
            const needsReprint = reprintSet.has(part.id);
            const canReuse = unchangedIds.has(part.id);
            return (
              <li
                key={part.id}
                className={needsReprint ? 'reprint-required' : canReuse ? 'reprint-reuse' : ''}
              >
                <label className="fixture-part-check">
                  <input
                    type="checkbox"
                    checked={selectedParts.includes(part.id)}
                    onChange={() => togglePart(part.id)}
                  />
                  <div>
                    <strong>{part.id}</strong> · {part.size_mm[0].toFixed(1)} × {part.size_mm[2].toFixed(1)} × {part.size_mm[1].toFixed(1)} mm · ~{part.estimated_filament_g.toFixed(0)} g / {part.estimated_print_minutes} min
                    <small>{part.printer_fit.detail}</small>
                    {needsReprint && <span className="diff-tag diff-reprint">Reprint required</span>}
                    {canReuse && <span className="diff-tag diff-reuse">Reuse existing print</span>}
                  </div>
                </label>
                <div className="fixture-part-links">
                  <a href={`${downloadBase}/parts/${part.id}.stl?revision=${revision}`} download>STL</a>
                  <a href={`${downloadBase}/parts/${part.id}.step?revision=${revision}`} download>STEP</a>
                </div>
              </li>
            );
          })}
        </ul>

        <div className="bench-station-downloads">
          <a href={packageUrl} download>Download selected package (ZIP)</a>
          <a href={`${downloadBase}/drawing.pdf?revision=${revision}`} download>2D Drawing &amp; Build Sheet (PDF)</a>
          <a href={`${downloadBase}/drawing.svg?revision=${revision}`} download>2D Drawing (SVG)</a>
          <a href={`${downloadBase}/e13-bench-checklist.md?revision=${revision}`} download>E13 Checklist (MD)</a>
        </div>
      </div>

      {panel === 'parts' && (
        <PurchasableBomPanel project={project} busy={busy} onSelectPart={onSelect} />
      )}

      {panel !== 'design' && (
        <div className="fixture-feedback-card">
          <h3>Measure -&gt; Revise -&gt; Reprint (Build Logs)</h3>
          <div className="build-log-form">
            <label>
              Fit outcome
              <select
                value={fitResult}
                onChange={(event) => setFitResult(event.target.value as BuildLogEntry['fit_result'])}
              >
                <option value="too_tight">Too tight</option>
                <option value="pass">Fits cleanly</option>
                <option value="too_loose">Too loose</option>
                <option value="warped">Warped</option>
                <option value="failed_print">Failed print</option>
              </select>
            </label>
            <label>
              Measured pocket length (mm, nominal {fixture.derived.pocket_length_mm.toFixed(2)})
              <input
                type="number"
                step="0.01"
                placeholder="e.g. 128.35"
                value={measuredMm}
                onChange={(event) => setMeasuredMm(event.target.value)}
              />
            </label>
            <label>
              Notes
              <input
                type="text"
                placeholder="Printer, corner fit, mast bolts..."
                value={notes}
                onChange={(event) => setNotes(event.target.value)}
              />
            </label>
            <button type="button" disabled={submitting || busy} onClick={() => void submitBuildLog()}>
              {submitting ? 'Saving…' : 'Record physical build log'}
            </button>
          </div>
          {formError && <p className="resource-error" role="alert">{formError}</p>}

          {(project.build_logs ?? []).length > 0 && (
            <ul className="build-logs-list">
              {(project.build_logs ?? []).slice().reverse().map((entry) => (
                <li key={entry.id}>
                  <div>
                    <strong>{entry.id}</strong> (rev {entry.revision}) · <span>{entry.fit_result}</span>
                    {entry.measured_features.map((feat, idx) => (
                      <small key={idx}>
                        {' '}· {feat.feature}: {feat.measured_mm.toFixed(2)} vs {feat.nominal_mm.toFixed(2)} mm
                      </small>
                    ))}
                  </div>
                  {(entry.fit_result === 'too_tight' || entry.fit_result === 'too_loose') &&
                    entry.applied_revision == null && (
                      <button
                        type="button"
                        disabled={submitting || busy}
                        onClick={() => void applyFeedbackRevision(entry.id)}
                      >
                        Apply clearance revision
                      </button>
                    )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
