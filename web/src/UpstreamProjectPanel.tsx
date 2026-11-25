import { AttachedCadModels } from './AttachedCad';
import { Bounds, OrbitControls, useBounds } from '@react-three/drei';
import { Canvas, useThree } from '@react-three/fiber';
import { Component as ReactComponent, Suspense, type ReactNode, useEffect, useState } from 'react';
import { Mesh, MeshStandardMaterial } from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import PurchasableBomPanel from './PurchasableBomPanel';
import type { Project, UpstreamProjectState } from './types';
import './upstream-panel.css';

const SCENE_SCALE_MM = 100;

class GlbErrorBoundary extends ReactComponent<
  { fallback: ReactNode; children: ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false };
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  render() {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}

function FitOnUpdate({ token }: { token: string }) {
  const bounds = useBounds();
  const size = useThree((state) => state.size);
  useEffect(() => {
    bounds.refresh().clip().fit();
  }, [bounds, size.width, size.height, token]);
  return null;
}

function LoadedGlb({
  url,
  color,
  onReady,
}: {
  url: string;
  color: string;
  onReady: () => void;
}) {
  const [scene, setScene] = useState<ReturnType<typeof Object.create> | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    const loader = new GLTFLoader();
    loader.load(
      url,
      (gltf) => {
        if (cancelled) return;
        const root = gltf.scene.clone(true);
        const mat = new MeshStandardMaterial({ color, metalness: 0.25, roughness: 0.55 });
        root.traverse((obj) => {
          if (obj instanceof Mesh) {
            obj.material = mat;
            obj.castShadow = true;
            obj.receiveShadow = true;
          }
        });
        setScene(root);
        onReady();
      },
      undefined,
      () => {
        if (!cancelled) setFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [url, color, onReady]);

  if (failed || !scene) return null;
  return <primitive object={scene} />;
}

export default function UpstreamProjectPanel({
  project,
  upstream,
  panel,
  busy = false,
  onProject,
}: {
  project: Project;
  upstream: UpstreamProjectState;
  panel: 'design' | 'parts' | 'instructions';
  busy?: boolean;
  onProject: (next: Project, assistantMessage?: string) => void;
}) {
  const [matingKind, setMatingKind] = useState<
    'adapter_plate' | 'bracket' | 'riser' | 'enclosure' | 'cutout_mount'
  >('adapter_plate');
  const [placement, setPlacement] = useState<'below' | 'above' | 'rear' | 'side_x' | 'surround'>(
    'below',
  );
  const [adapting, setAdapting] = useState(false);
  const [adaptError, setAdaptError] = useState<string | null>(null);
  const [fitToken, setFitToken] = useState('init');

  const revision = project.revision;
  const prov = upstream.provenance;
  const geom = upstream.geometry;
  const adapt = upstream.adaptation;
  const basePreview = `/api/projects/${encodeURIComponent(project.id)}/upstream/preview`;
  const dlBase = `/api/projects/${encodeURIComponent(project.id)}/upstream/download`;
  const upstreamGlbUrl = `${basePreview}/upstream.glb?revision=${revision}`;
  const customGlbUrl = adapt ? `${basePreview}/custom.glb?revision=${revision}` : null;

  async function generateAdaptation() {
    if (adapting || busy) return;
    setAdapting(true);
    setAdaptError(null);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(project.id)}/upstream/adapt`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            project_revision: revision,
            mode: 'mating',
            mating_spec: { kind: matingKind, placement, clearance_mm: 0.4 },
          }),
        },
      );
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.project) {
        throw new Error(
          typeof data?.detail === 'string' ? data.detail : 'CAD adaptation request failed.',
        );
      }
      onProject(data.project as Project, data.assistant_message as string | undefined);
    } catch (reason: unknown) {
      setAdaptError(reason instanceof Error ? reason.message : 'CAD adaptation failed.');
    } finally {
      setAdapting(false);
    }
  }

  const appSize = geom.app_size_mm ?? [80, 40, 60];
  const fallbackBox = (
    <mesh position={[0, appSize[1] / (2 * SCENE_SCALE_MM), 0]}>
      <boxGeometry
        args={[
          Math.max(appSize[0] / SCENE_SCALE_MM, 0.1),
          Math.max(appSize[1] / SCENE_SCALE_MM, 0.1),
          Math.max(appSize[2] / SCENE_SCALE_MM, 0.1),
        ]}
      />
      <meshStandardMaterial color="#64748b" wireframe />
    </mesh>
  );

  return (
    <section
      className={`upstream-panel upstream-panel-${panel}`}
      aria-label="Open-source hardware project"
    >
      <div className="upstream-header">
        <div>
          <h2>{upstream.title || prov.repo_full_name}</h2>
          <p>
            <a href={prov.repo_url} target="_blank" rel="noopener noreferrer">
              {prov.repo_full_name}
            </a>{' '}
            · commit <code>{prov.commit_sha.slice(0, 10)}</code> · <code>{prov.file_path}</code>
          </p>
        </div>
        <div className="upstream-badges">
          <span className={`license-badge license-${prov.license_category}`}>{prov.spdx_id}</span>
          <span className="hash-badge">SHA-256 {prov.file_sha256.slice(0, 10)}</span>
        </div>
      </div>

      <div hidden={panel !== 'design'} className="upstream-design-stage">
        <div className="upstream-viewport">
          <Canvas
            aria-label="Interactive open-source hardware 3D preview"
            orthographic
            frameloop="demand"
            camera={{ position: [2.2, 1.8, 2.4], zoom: 160, near: 0.01, far: 100 }}
          >
            <color attach="background" args={['#eef3f4']} />
            <ambientLight intensity={1.5} />
            <directionalLight position={[3, 5, 4]} intensity={2.2} />
            <directionalLight position={[-3, 2, -2]} intensity={1.1} />
            <GlbErrorBoundary fallback={fallbackBox}>
              <Suspense fallback={fallbackBox}>
                <Bounds fit clip observe margin={1.2} maxDuration={0}>
                  <AttachedCadModels project={project} scale={0.01} />
                  <group scale={[10, 10, 10]}>
                    <LoadedGlb
                      url={upstreamGlbUrl}
                      color="#64748b"
                      onReady={() => setFitToken(`up-${revision}`)}
                    />
                    {customGlbUrl && (
                      <LoadedGlb
                        url={customGlbUrl}
                        color="#0d9488"
                        onReady={() => setFitToken(`cust-${revision}`)}
                      />
                    )}
                  </group>
                  <FitOnUpdate token={`${revision}-${fitToken}`} />
                </Bounds>
              </Suspense>
            </GlbErrorBoundary>
            <OrbitControls makeDefault />
          </Canvas>
        </div>

        <div className="upstream-metrics">
          <span>
            <strong>Normalized size:</strong> {appSize.map((v) => v.toFixed(1)).join(' x ')} mm
          </span>
          <span>
            <strong>CAD format:</strong> {geom.cad_kind}
          </span>
          {adapt && (
            <span className={adapt.interference.passed ? 'status-ok' : 'status-bad'}>
              <strong>Interference check:</strong>{' '}
              {adapt.interference.passed
                ? `Passed (${adapt.interference.interference_volume_mm3} mm^3)`
                : `Overlap detected (${adapt.interference.interference_volume_mm3} mm^3)`}
            </span>
          )}
          {adapt?.printer_fit && (
            <span className={adapt.printer_fit.fits ? 'status-ok' : 'status-bad'}>
              {adapt.printer_fit.detail}
            </span>
          )}
        </div>

        <div className="upstream-adapt-controls">
          <label>
            Mating type
            <select
              value={matingKind}
              onChange={(e) => setMatingKind(e.target.value as typeof matingKind)}
              disabled={adapting || busy}
            >
              <option value="adapter_plate">Adapter plate</option>
              <option value="riser">Riser</option>
              <option value="bracket">Bracket</option>
              <option value="enclosure">Protective enclosure</option>
              <option value="cutout_mount">Cutout mount</option>
            </select>
          </label>
          <label>
            Placement
            <select
              value={placement}
              onChange={(e) => setPlacement(e.target.value as typeof placement)}
              disabled={adapting || busy}
            >
              <option value="below">Below</option>
              <option value="above">Above</option>
              <option value="rear">Rear</option>
              <option value="side_x">Side (+X)</option>
              <option value="surround">Surround</option>
            </select>
          </label>
          <button
            type="button"
            className="confirm-button"
            onClick={() => void generateAdaptation()}
            disabled={adapting || busy}
          >
            {adapting ? 'Generating CAD...' : 'Generate mating adaptation'}
          </button>
          <a className="brief-download" href={`${dlBase}/package.zip?revision=${revision}`}>
            Download build package (.zip)
          </a>
        </div>
        {adaptError && <p className="upstream-error">{adaptError}</p>}
      </div>

      <div hidden={panel !== 'parts'} className="upstream-parts-stage">
        <div className="upstream-actions-row">
          <a className="brief-download" href={`${dlBase}/bom.csv?revision=${revision}`}>
            Download BOM (.csv)
          </a>
          <a
            className="brief-download"
            href={`${dlBase}/THIRD_PARTY_NOTICES.md?revision=${revision}`}
          >
            THIRD_PARTY_NOTICES.md
          </a>
        </div>
        <table className="plan-table">
          <thead>
            <tr>
              <th>Part</th>
              <th>Qty</th>
              <th>Origin</th>
              <th>Compatibility</th>
              <th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {upstream.bom.length === 0 ? (
              <tr>
                <td colSpan={5}>No structured BOM items found in this repository.</td>
              </tr>
            ) : (
              upstream.bom.map((item) => (
                <tr key={item.id}>
                  <td>{item.name}</td>
                  <td>{item.quantity}</td>
                  <td>
                    <span className={`origin-pill origin-${item.source}`}>
                      {item.source === 'upstream_repo' ? 'Upstream repo' : 'AutoLab custom'}
                    </span>
                  </td>
                  <td>{item.compatibility_status}</td>
                  <td>{item.notes ?? item.supplier ?? item.source_file}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        <PurchasableBomPanel project={project} busy={busy} />
      </div>

      <div hidden={panel !== 'instructions'} className="upstream-docs-stage">
        <div className="upstream-actions-row">
          <a className="brief-download" href={`${dlBase}/build-guide.md?revision=${revision}`}>
            Download build guide (.md)
          </a>
          <a className="brief-download" href={`${dlBase}/package.zip?revision=${revision}`}>
            Download complete archive (.zip)
          </a>
        </div>
        <h3>Upstream Assembly Steps ({prov.repo_full_name})</h3>
        <ol>
          {upstream.assembly.upstream_steps.length === 0 ? (
            <li>Consult the upstream repository documentation at commit {prov.commit_sha}.</li>
          ) : (
            upstream.assembly.upstream_steps.map((step) => (
              <li key={`up-${step.order}`}>
                {step.text} <small>({step.source_file})</small>
              </li>
            ))
          )}
        </ol>
        {upstream.assembly.custom_steps.length > 0 && (
          <>
            <h3>AutoLab Custom Adaptation Steps</h3>
            <ol>
              {upstream.assembly.custom_steps.map((step) => (
                <li key={`cu-${step.order}`}>
                  {step.text} <small>({step.source_file})</small>
                </li>
              ))}
            </ol>
          </>
        )}
        <h3>Software &amp; Firmware References ({upstream.software.execution_status})</h3>
        <p>{upstream.software.disclaimer}</p>
        <p>
          <strong>Detected stacks:</strong>{' '}
          {upstream.software.detected_stacks.length
            ? upstream.software.detected_stacks.join(', ')
            : 'None detected'}
        </p>
      </div>
    </section>
  );
}
