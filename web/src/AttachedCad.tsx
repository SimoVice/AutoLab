import './attached-cad.css';
import { Html, useBounds } from '@react-three/drei';
import { useThree } from '@react-three/fiber';
import { useEffect, useMemo, useState } from 'react';
import { Box3, Group, Matrix4, Mesh, MeshStandardMaterial, Vector3 } from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import type { CadBounds, CadPlacementCheck, CadVector, Project } from './types';

function assetUrl(project: Project, name: string, role: string) {
  return `/api/projects/${encodeURIComponent(project.id)}/upstream/instances/${encodeURIComponent(name)}/${role}?revision=${project.revision}`;
}

function CadMesh({ url, geometry, adapter }: { url: string; geometry: CadBounds; adapter?: boolean }) {
  const invalidate = useThree((state) => state.invalidate);
  const [model, setModel] = useState<Group | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let current = true;
    let release: (() => void) | undefined;
    setModel(null);
    setFailed(false);
    new GLTFLoader().load(url, ({ scene }) => {
      const meshes: Mesh[] = [];
      scene.traverse((obj) => { if (obj instanceof Mesh) meshes.push(obj); });
      const materials = new Set(meshes.flatMap(mesh => Array.isArray(mesh.material) ? mesh.material : [mesh.material]));
      const finish = new MeshStandardMaterial({ color: adapter ? '#b97533' : '#487b87', roughness: 0.58 });
      for (const mesh of meshes) mesh.material = finish;
      release = () => {
        for (const mesh of meshes) mesh.geometry.dispose();
        for (const material of materials) material.dispose();
        finish.dispose();
      };
      if (!current) { release(); return; }
      setModel(scene);
      invalidate();
    }, undefined, () => { if (current) { setFailed(true); invalidate(); } });
    return () => { current = false; release?.(); };
  }, [url, adapter, invalidate]);
  const lo = geometry.cad_min_mm, hi = geometry.cad_max_mm;
  const centre: CadVector = [(lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2, -(lo[1] + hi[1]) / 2];
  if (model) return <primitive object={model} scale={1000} dispose={null} />;
  return <group position={centre}>
    <mesh><boxGeometry args={geometry.app_size_mm} /><meshBasicMaterial wireframe color="#58767d" /></mesh>
    {failed && <Html center><small className="cad-preview-status">Preview unavailable</small></Html>}
  </group>;
}

function PlacedCad({ project, check }: { project: Project; check: CadPlacementCheck }) {
  const source = project.cad_instances![check.name];
  const matrix = useMemo(() => {
    const [a, b, c] = check.world_rotation_matrix;
    const [x, y, z] = check.world_translation_mm;
    return new Matrix4().set(a[0], a[1], a[2], x, b[0], b[1], b[2], y, c[0], c[1], c[2], z, 0, 0, 0, 1);
  }, [check]);
  return <group matrix={matrix} matrixAutoUpdate={false} name={check.name}>
    <CadMesh url={assetUrl(project, check.name, 'preview')} geometry={source.geometry} />
    {source.adaptation && <CadMesh url={assetUrl(project, check.name, 'adapter-preview')} geometry={source.adaptation.custom_geometry} adapter />}
  </group>;
}

export function AttachedCadFraming({ project, scale, origin, turn }: {
  project?: Project; scale: number; origin: CadVector; turn: number;
}) {
  const bounds = useBounds();
  const { width, height } = useThree(state => state.size);
  const envelope = project?.design.spatial_fit?.cad_assembly?.bounds_mm;
  const [x, y, z] = origin;
  useEffect(() => {
    if (!envelope || width < 40 || height < 40) return;
    const offset = new Vector3(x, y, z);
    const [low, high] = envelope.map(corner => new Vector3(...corner).sub(offset).multiplyScalar(scale));
    const box = new Box3(low, high).applyMatrix4(new Matrix4().makeRotationY(turn));
    const frame = requestAnimationFrame(() => bounds.refresh(box).reset().clip().fit());
    return () => cancelAnimationFrame(frame);
  }, [envelope, bounds, scale, x, y, z, turn, width, height]);
  return null;
}

export function AttachedCadModels({ project, scale, origin = [0, 0, 0], host }: {
  project?: Project; scale: number; origin?: CadVector; host?: string;
}) {
  const checks = project?.design.spatial_fit?.cad_assembly?.instances;
  if (!project?.cad_instances || !checks?.length) return null;
  return <group scale={scale} position={origin.map(v => -v * scale) as CadVector}>
    {checks.filter(check => host === undefined || check.host === host).map(check => <PlacedCad key={check.name} project={project} check={check} />)}
  </group>;
}

function handoffUrl(project: Project, asset: string) {
  return `/api/projects/${encodeURIComponent(project.id)}/handoff/${asset}?revision=${project.revision}`;
}

export function CadInstanceDetails({ project }: { project: Project }) {
  const fit = project.design.spatial_fit?.cad_assembly;
  if (!fit || !project.cad_instances) return null;
  return <details className="cad-instance-details">
    <summary>Attached CAD ({fit.instances.length})</summary>
    {fit.instances.map(check => {
      const source = project.cad_instances![check.name], pin = source.provenance;
      const units = source.source_units === 'auto' ? source.geometry.detected_units : source.source_units;
      return <section key={check.name} aria-label={check.name}>
        <h3>{check.name}</h3>
        <p>{source.placement.datum} · {source.placement.parent} · {source.placement.translation_mm.join(', ')} mm</p>
        <p><a href={pin.raw_url} target="_blank" rel="noreferrer">{pin.repo_full_name}</a> · {pin.spdx_id} · {units}</p>
        <details><summary>Source file and hashes</summary><p>{pin.file_path}</p><p>Commit <code>{pin.commit_sha}</code></p><p>SHA-256 <code>{pin.file_sha256}</code></p></details>
        <p>Access at rest · {check.access_status === 'review' ? 'Review clearance against ' + check.access_blockers.join(', ') : check.access_status === 'clear' ? 'Clear within the specified corridor' : 'No access corridor specified'}</p>
        {check.mating_interference && <p>Source and adapter · {check.mating_interference.interference_volume_mm3} mm³ solid overlap · {check.mating_interference.passed ? 'Pass' : 'Interference'}</p>}
        {source.adaptation && <p>{check.printer_fit?.detail ?? 'Printer fit requires a configured build volume.'}</p>}
        <a download href={assetUrl(project, check.name, 'original')}>Original CAD</a>
        {source.adaptation && <> · <a download href={assetUrl(project, check.name, 'adapter-step')}>Adapter STEP</a> · <a download href={assetUrl(project, check.name, 'adapter-stl')}>Adapter STL</a></>}
      </section>;
    })}
    {project.mating_change && <p>Revision {project.mating_change.revision} · Reprint the adapter for {project.mating_change.instance} and check its mounting datum.</p>}
    <p>Rest-pose envelope overlaps · {fit.possible_overlaps.length ? fit.possible_overlaps.map(pair => pair.join(' / ')).join(', ') : 'None detected among placed parts'}</p>
    {fit.unplaced_parts.length > 0 && <p>Parts without a checked placement · {fit.unplaced_parts.join(', ')}</p>}
    <nav aria-label="Assembly downloads">
      <a href={handoffUrl(project, 'package.zip')} download>Complete assembly package (ZIP)</a>
      {' · '}<a href={handoffUrl(project, 'cad/assembly.step')} download>Assembly STEP</a>
      {' · '}<a href={handoffUrl(project, 'cad/assembly.stl')} download>Assembly STL</a>
      {' · '}<a href={handoffUrl(project, 'purchasing/bom.csv')} download>Purchasing list</a>
      {' · '}<a href={handoffUrl(project, 'assembly/guide.md')} download>Installation guide</a>
      {' · '}<a href={`/api/projects/${encodeURIComponent(project.id)}/snapshot?revision=${project.revision}`} download>Project inputs (JSON)</a>
    </nav>
  </details>;
}
