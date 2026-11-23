import { useEffect, useState } from 'react';
import type { LabProfile, PrinterPreset, Project } from './types';

function emptyProfile(): LabProfile {
  return {
    schema_version: '1',
    lab_name: null,
    printer: { name: null, build_volume_mm: null, nozzle_mm: null, materials: [] },
    bench: {
      width_mm: null,
      depth_mm: null,
      height_mm: null,
      front_clearance_mm: null,
      rear_clearance_mm: null,
      side_clearance_mm: null,
      notes: null,
    },
    measuring_tools: [],
    owned_equipment: [],
    notes: null,
    updated_at: null,
  };
}

function parseOptionalNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function parseCsvList(raw: string): string[] {
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

export default function LabProfilePanel({
  project,
  disabled,
  onApplied,
  onClose,
}: {
  project: Project | null;
  disabled: boolean;
  onApplied: (updated: Project, notice: string) => void;
  onClose: () => void;
}) {
  const [profile, setProfile] = useState<LabProfile>(emptyProfile);
  const [presets, setPresets] = useState<PrinterPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusNote, setStatusNote] = useState<string | null>(null);

  const [labName, setLabName] = useState('');
  const [printerName, setPrinterName] = useState('');
  const [printerX, setPrinterX] = useState('');
  const [printerY, setPrinterY] = useState('');
  const [printerZ, setPrinterZ] = useState('');
  const [nozzle, setNozzle] = useState('');
  const [materials, setMaterials] = useState('');
  const [benchW, setBenchW] = useState('');
  const [benchD, setBenchD] = useState('');
  const [benchH, setBenchH] = useState('');
  const [tools, setTools] = useState('');
  const [equipment, setEquipment] = useState('');

  function syncDraftFields(loaded: LabProfile) {
    setProfile(loaded);
    setLabName(loaded.lab_name ?? '');
    setPrinterName(loaded.printer.name ?? '');
    setPrinterX(loaded.printer.build_volume_mm ? String(loaded.printer.build_volume_mm[0]) : '');
    setPrinterY(loaded.printer.build_volume_mm ? String(loaded.printer.build_volume_mm[1]) : '');
    setPrinterZ(loaded.printer.build_volume_mm ? String(loaded.printer.build_volume_mm[2]) : '');
    setNozzle(loaded.printer.nozzle_mm != null ? String(loaded.printer.nozzle_mm) : '');
    setMaterials(loaded.printer.materials.join(', '));
    setBenchW(loaded.bench.width_mm != null ? String(loaded.bench.width_mm) : '');
    setBenchD(loaded.bench.depth_mm != null ? String(loaded.bench.depth_mm) : '');
    setBenchH(loaded.bench.height_mm != null ? String(loaded.bench.height_mm) : '');
    setTools(loaded.measuring_tools.join(', '));
    setEquipment(loaded.owned_equipment.join(', '));
  }

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const resp = await fetch('/api/lab-profile', { cache: 'no-store' });
        const data = (await resp.json()) as { profile: LabProfile; presets: PrinterPreset[] };
        if (active && resp.ok) {
          syncDraftFields(data.profile);
          setPresets(data.presets ?? []);
        }
      } catch {
        if (active) setError('Could not load saved lab profile.');
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  function applyPreset(preset: PrinterPreset) {
    setPrinterName(preset.name);
    setPrinterX(String(preset.build_volume_mm[0]));
    setPrinterY(String(preset.build_volume_mm[1]));
    setPrinterZ(String(preset.build_volume_mm[2]));
    setNozzle(String(preset.nozzle_mm));
    setMaterials(preset.materials.join(', '));
    setStatusNote(`Loaded preset ${preset.name}; click Save lab profile to store.`);
  }

  function buildPayload(): LabProfile {
    const x = parseOptionalNumber(printerX);
    const y = parseOptionalNumber(printerY);
    const z = parseOptionalNumber(printerZ);
    const buildVolume: [number, number, number] | null = x && y && z ? [x, y, z] : null;
    if ((printerX.trim() || printerY.trim() || printerZ.trim()) && !buildVolume) {
      throw new Error('Provide positive numbers for all three printer X, Y, and Z axes.');
    }
    return {
      schema_version: '1',
      lab_name: labName.trim() || null,
      printer: {
        name: printerName.trim() || null,
        build_volume_mm: buildVolume,
        nozzle_mm: parseOptionalNumber(nozzle),
        materials: parseCsvList(materials),
      },
      bench: {
        width_mm: parseOptionalNumber(benchW),
        depth_mm: parseOptionalNumber(benchD),
        height_mm: parseOptionalNumber(benchH),
        front_clearance_mm: profile.bench.front_clearance_mm,
        rear_clearance_mm: profile.bench.rear_clearance_mm,
        side_clearance_mm: profile.bench.side_clearance_mm,
        notes: profile.bench.notes,
      },
      measuring_tools: parseCsvList(tools),
      owned_equipment: parseCsvList(equipment),
      notes: profile.notes,
      updated_at: profile.updated_at,
    };
  }

  async function saveProfile(applyToProject: boolean) {
    setError(null);
    setStatusNote(null);
    setSaving(true);
    try {
      const payload = buildPayload();
      const saveResp = await fetch('/api/lab-profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const saved = (await saveResp.json()) as { profile?: LabProfile; detail?: string };
      if (!saveResp.ok || !saved.profile) {
        throw new Error(saved.detail ?? 'Could not save lab profile.');
      }
      syncDraftFields(saved.profile);
      if (applyToProject && project) {
        const applyResp = await fetch(`/api/projects/${encodeURIComponent(project.id)}/apply-lab-profile`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_revision: project.revision }),
        });
        const applied = (await applyResp.json()) as { project?: Project; assistant_message?: string; detail?: string };
        if (!applyResp.ok || !applied.project) {
          throw new Error(applied.detail ?? 'Saved lab profile, but could not apply to project.');
        }
        onApplied(applied.project, applied.assistant_message ?? 'Applied laboratory profile.');
        setStatusNote('Saved lab profile and applied bench dimensions to the current project.');
      } else {
        setStatusNote('Saved lab profile locally.');
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Failed to save lab profile.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="lab-profile-backdrop" role="dialog" aria-modal="true" aria-labelledby="lab-profile-title">
      <div className="lab-profile-modal">
        <div className="lab-profile-header">
          <div>
            <h2 id="lab-profile-title">Laboratory &amp; printer profile</h2>
            <p>Stored locally across projects so designs respect your 3D printer, tools, and bench limits.</p>
          </div>
          <button type="button" className="new-project-button" onClick={onClose}>Close</button>
        </div>
        {loading ? (
          <p className="lab-profile-note">Loading laboratory profile…</p>
        ) : (
          <div className="lab-profile-form">
            {presets.length > 0 && (
              <div className="lab-profile-presets">
                <span>Printer presets:</span>
                {presets.map((preset) => (
                  <button key={preset.id} type="button" className="new-project-button" onClick={() => applyPreset(preset)}>
                    {preset.name}
                  </button>
                ))}
              </div>
            )}
            <label>
              <span>Lab name</span>
              <input type="text" value={labName} onChange={(e) => setLabName(e.target.value)} placeholder="e.g. Tissue Culture Bay 2" />
            </label>
            <label>
              <span>3D printer model</span>
              <input type="text" value={printerName} onChange={(e) => setPrinterName(e.target.value)} placeholder="e.g. Bambu Lab A1 mini" />
            </label>
            <div className="lab-profile-grid3">
              <label>
                <span>Build X (mm)</span>
                <input type="number" min="1" step="any" value={printerX} onChange={(e) => setPrinterX(e.target.value)} placeholder="180" />
              </label>
              <label>
                <span>Build Y (mm)</span>
                <input type="number" min="1" step="any" value={printerY} onChange={(e) => setPrinterY(e.target.value)} placeholder="180" />
              </label>
              <label>
                <span>Build Z (mm)</span>
                <input type="number" min="1" step="any" value={printerZ} onChange={(e) => setPrinterZ(e.target.value)} placeholder="180" />
              </label>
            </div>
            <div className="lab-profile-grid2">
              <label>
                <span>Nozzle diameter (mm)</span>
                <input type="number" min="0.1" step="0.1" value={nozzle} onChange={(e) => setNozzle(e.target.value)} placeholder="0.4" />
              </label>
              <label>
                <span>Available filaments</span>
                <input type="text" value={materials} onChange={(e) => setMaterials(e.target.value)} placeholder="PLA, PETG" />
              </label>
            </div>
            <div className="lab-profile-grid3">
              <label>
                <span>Bench width (mm)</span>
                <input type="number" min="1" step="any" value={benchW} onChange={(e) => setBenchW(e.target.value)} placeholder="600" />
              </label>
              <label>
                <span>Bench depth (mm)</span>
                <input type="number" min="1" step="any" value={benchD} onChange={(e) => setBenchD(e.target.value)} placeholder="500" />
              </label>
              <label>
                <span>Overhead height (mm)</span>
                <input type="number" min="1" step="any" value={benchH} onChange={(e) => setBenchH(e.target.value)} placeholder="700" />
              </label>
            </div>
            <label>
              <span>Measuring tools</span>
              <input type="text" value={tools} onChange={(e) => setTools(e.target.value)} placeholder="Digital caliper (0.02 mm), ruler" />
            </label>
            <label>
              <span>Owned cameras, balances, or robots</span>
              <input type="text" value={equipment} onChange={(e) => setEquipment(e.target.value)} placeholder="USB inspection camera, Ohaus balance" />
            </label>
            {error && <p className="lab-profile-error" role="alert">{error}</p>}
            {statusNote && <p className="lab-profile-note" role="status">{statusNote}</p>}
            <div className="lab-profile-actions">
              <button type="button" className="confirm-button" disabled={saving || disabled} onClick={() => void saveProfile(false)}>
                {saving ? 'Saving…' : 'Save lab profile'}
              </button>
              {project && (
                <button type="button" className="new-project-button" disabled={saving || disabled} onClick={() => void saveProfile(true)}>
                  Save &amp; apply bench limits to project
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
