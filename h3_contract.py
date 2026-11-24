"""Deterministic, fail-closed MiniMax H3 R2V production contract.
The workflow API JSON must be exported from the official GUI template.
Do not attempt to submit the GUI JSON directly to ComfyUI /prompt.
"""
from __future__ import annotations
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

# --- Drive ID resolution (TASK-RELEASE-V032 R4 desensitization) --------
# Real Google Drive file/folder IDs are NEVER committed: the public tree
# carries only EXAMPLE_* placeholders. A live run resolves the real IDs
# in this precedence order (first non-empty value wins):
#   1. H3_DRIVE_ID_<KEY> environment variable (per-run override),
#   2. model_cache/local_drive_ids.json (gitignored, must be mode 0600;
#      a file with group/other permission bits is ignored with a warning),
#   3. the EXAMPLE_* offline/test default below.
# The local file is read at import time; a missing file is normal in a
# fresh checkout and is silently ignored.
LOCAL_DRIVE_IDS_PATH = Path(__file__).resolve().parent / "model_cache" / "local_drive_ids.json"

DRIVE_ID_DEFAULTS = {
    "POSTER": "EXAMPLE_DRIVE_POSTER_ID",
    "STORY": "EXAMPLE_DRIVE_STORY_ID",
    "DEPOT_ROOT": "EXAMPLE_DRIVE_DEPOT_ROOT_ID",
    "READY_MANIFEST": "EXAMPLE_DRIVE_READY_MANIFEST_ID",
    "MODEL_DIFFUSION": "EXAMPLE_DRIVE_MODEL_DIFFUSION_ID",
    "MODEL_TEXT_ENCODER": "EXAMPLE_DRIVE_MODEL_TEXT_ENCODER_ID",
    "MODEL_VIDEO_VAE": "EXAMPLE_DRIVE_MODEL_VIDEO_VAE_ID",
    "MODEL_AUDIO_VAE": "EXAMPLE_DRIVE_MODEL_AUDIO_VAE_ID",
    "LEGACY_VIDEO_VAE": "EXAMPLE_DRIVE_LEGACY_VIDEO_VAE_ID",
    "LEGACY_AUDIO_VAE": "EXAMPLE_DRIVE_LEGACY_AUDIO_VAE_ID",
    "MODEL_DIFFUSION_FOLDER": "EXAMPLE_DRIVE_MODEL_DIFFUSION_FOLDER_ID",
    "MODEL_TEXT_ENCODER_FOLDER": "EXAMPLE_DRIVE_MODEL_TEXT_ENCODER_FOLDER_ID",
    "MODEL_VAE_FOLDER": "EXAMPLE_DRIVE_MODEL_VAE_FOLDER_ID",
    "COMFYUI_CACHE_FOLDER": "EXAMPLE_DRIVE_COMFYUI_CACHE_FOLDER_ID",
    "COMFYUI_TARBALL": "EXAMPLE_DRIVE_COMFYUI_TARBALL_ID",
    "COMFYUI_TARBALL_SHA256": "EXAMPLE_DRIVE_COMFYUI_TARBALL_SHA256_ID",
    "COMFYUI_SOURCE_MANIFEST": "EXAMPLE_DRIVE_COMFYUI_SOURCE_MANIFEST_ID",
    "VIDEO_OUTPUTS_FOLDER": "EXAMPLE_DRIVE_VIDEO_OUTPUTS_FOLDER_ID",
    "VIDEO_A_MP4": "EXAMPLE_DRIVE_VIDEO_A_MP4_ID",
    "DEPOT_CANARY": "EXAMPLE_DRIVE_DEPOT_CANARY_ID",
}


def is_example_drive_id(value) -> bool:
    """True for the committed EXAMPLE_* placeholders (never a real asset)."""
    return isinstance(value, str) and value.startswith("EXAMPLE_DRIVE_")


def looks_like_real_drive_id(value) -> bool:
    """Shape check for a real Drive ID: 33 chars, starts with '1', mixed
    case (this excludes EXAMPLE_* placeholders, hashes, test fake ids
    and local `NN-<slug>.md` incident filenames). Mirrors the
    drive-file-id rule in scripts/secret_scan.py; keep both in sync."""
    if not isinstance(value, str) or len(value) != 33 or not value.startswith("1"):
        return False
    if not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
               for c in value):
        return False
    return any(c.isupper() for c in value) and any(c.islower() for c in value)


def load_local_drive_ids(path=None) -> dict:
    """Read the gitignored local ID file. Returns {} when the file is
    absent (fresh checkout), unreadable, malformed, or too permissive.
    A too-permissive file is ignored with a stderr warning, never used:
    real asset IDs must not sit in a group/other-readable file."""
    path = Path(path) if path is not None else LOCAL_DRIVE_IDS_PATH
    try:
        if not path.is_file():
            return {}
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            print(f"WARNING: {path} has permissions {mode:03o}; refusing to "
                  f"use it (must be 0600). Configure H3_DRIVE_ID_* env vars "
                  f"or fix the permissions.", file=sys.stderr)
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"WARNING: ignoring unreadable local Drive ID file {path}: {e}",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"WARNING: ignoring local Drive ID file {path}: not a JSON object",
              file=sys.stderr)
        return {}
    return {str(k): v for k, v in data.items()
            if isinstance(v, str) and v.strip()}


def resolve_drive_id(key: str, default=None) -> str:
    """Resolve one Drive ID: H3_DRIVE_ID_<KEY> env var, then the local
    gitignored file, then `default` or the EXAMPLE_* default for the key.
    Never raises for a missing source; the EXAMPLE_* default keeps
    offline tests and fresh checkouts working."""
    env_value = os.environ.get("H3_DRIVE_ID_" + key, "").strip()
    if env_value:
        return env_value
    local = load_local_drive_ids()
    if isinstance(local.get(key), str) and local[key].strip():
        return local[key].strip()
    if default is not None:
        return default
    return DRIVE_ID_DEFAULTS.get(key, "")


POSTER_SHA256 = 'bd6d45312f340234d0fa06af0de5284949b2196f280677dd44468a300075c7bb'
# Offline/test defaults are EXAMPLE_* placeholders (v0.3.2 R4). Live runs
# resolve the real IDs via resolve_drive_id (env / local_drive_ids.json).
POSTER_DRIVE_ID = resolve_drive_id("POSTER")
STORY_DRIVE_ID = resolve_drive_id("STORY")
# Pinned Hugging Face revision of the four model files (CACHE_READY
# 2026-11-10; READY manifest Drive ID is EXAMPLE_DRIVE_READY_MANIFEST_ID
# in this public tree — the real ID lives only in local_drive_ids.json).
# Previously a bare literal in h3_controller.archive_variant_to_drive;
# named here so the environment manifest and the upload path share one
# source of truth (TASK-005 §6, Issue #29).
MODEL_HF_REVISION = '54472221d27c695fca9621b0c3ed52f06c4af707'
WIDTH, HEIGHT, FPS = 736, 1312, 24
ANIMATION_FRAMES, POSTER_FRAMES = 226, 48
TOTAL_FRAMES = ANIMATION_FRAMES + POSTER_FRAMES
STEPS = 25
MODEL_FILES = {
    'diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors',
    'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
    'vae/minimax_h3_video_vae_int8_convrot.safetensors',
    'vae/minimax_h3_audio_vae_fp32.safetensors',
}

ASSETS = {
 'leopard': 'Q版猎豹吉祥物，穿红金斗牛士服、戴滑雪镜式护目镜、手持大红斗篷，圆头圆脑、豹纹斑点、表情自信潇洒',
 'bull': 'Q版大公牛，深色毛皮、牛角锋利、低头刨地、鼻孔喷气、力量感强',
 'arena': '阳光明媚的西班牙斗牛场，圆形看台、拥挤观众、红旗、黄沙地面',
 'ball': '标准橙色篮球、黑色纹路、Q版卡通材质',
}
BASE = ('Use <Picture 1> as the exact VISUAL IDENTITY and Q-style reference for the leopard hero, red-and-gold bullfighter suit, blue goggles, red cape and arena. '
        'Use <Picture 2> only as a supplementary style/character reference; both images show the same original poster. '
        'Do NOT draw poster titles, logos, scoreboard text or event dates during the animated section. '
        'Vertical 9:16, bright chibi anime, stable character design, continuous coherent action, no speech or subtitles. '
        'Characters: {leopard}. {bull}. Arena: {arena}. Basketball: {ball}. '
        'Audio only: hoofbeats, bull breathing, cape swishes, rushing air, arena cheer, no dialogue, no copyrighted music. ')
SCENES = {
 'A': ('FIRST (0-2s) calm leopard bullfighter stands alone in the arena. Bull snorts and scrapes sand; leopard smiles and flicks the red cape. '
       'SECOND (2-5s) bull CHARGES, leopard sidesteps once, cape billows, bull misses and runs past. '
       'THIRD (5-7.5s) bull CHARGES A SECOND TIME, leopard makes a second distinct elegant dodge and the bull misses again. '
       'FINAL (7.5-9.4s) leopard gathers cape, turns toward camera, bull leaves frame, leopard raises one orange basketball in triumph. Continuous action, no cuts to title card.'),
 'B': ('FIRST (0-2s) bull rushes toward the leopard; leopard runs awkwardly two steps, briefly loses composure, without falling. '
       'SECOND (2-4s) leopard stops, recovers confidence, smiles and flicks the red cape. '
       'THIRD (4-7s) bull CHARGES AGAIN, leopard makes a crisp sidestep and the bull misses, sand swirls. '
       'FINAL (7-9.4s) leopard gathers cape, turns toward camera, bull leaves frame, leopard raises one orange basketball triumphantly. Continuous action, no title card.'),
}

def sha256_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        for part in iter(lambda:f.read(4*1024*1024), b''): h.update(part)
    return h.hexdigest()

def prompt_for(variant: str) -> str:
    if variant not in SCENES: raise ValueError('variant must be A or B')
    return BASE.format(**ASSETS) + SCENES[variant]

def validate_poster(p: Path) -> None:
    if not p.is_file() or sha256_file(p) != POSTER_SHA256:
        raise ValueError('Original poster missing or SHA256 mismatch: '+str(p))

def validate_ref_image(p: Path, sha256: str) -> None:
    """Generic reference-image check: the file must exist and its
    SHA-256 must equal the spec value. validate_poster above is the
    frozen A/B fixture case of this check (POSTER_SHA256)."""
    if not Path(p).is_file():
        raise ValueError('Reference image missing: '+str(p))
    if sha256_file(Path(p)).lower() != sha256.lower():
        raise ValueError('Reference image SHA256 mismatch: '+str(p))

def validate_model_cache_manifest(d: dict) -> None:
    """Metadata alone or CACHE_READY.json flag is insufficient.
    `server_verified` requires independent Drive API retrieval using SAME account,
    not FUSE copy completion. Full source SHA256 and remote ID required.
    """
    files=d.get('files', [])
    if set(x.get('path') for x in files)!=MODEL_FILES:
        raise ValueError('Model cache requires exactly four official Ref2VA files')
    if d.get('status')!='READY' or d.get('verification_method')!='drive_v3_server_api':
        raise ValueError('Must be server-verified READY, not historical cache flag')
    if not d.get('account_verified') or not d.get('verified_at_utc'):
        raise ValueError('Same account / verification timestamp not established')
    for f in files:
        if not isinstance(f.get('size'),int) or f['size']<=0: raise ValueError('Invalid model size')
        if not isinstance(f.get('drive_file_id'),str) or not f['drive_file_id']: raise ValueError('Missing Drive file ID')
        checksum=f.get('sha256')
        if not isinstance(checksum,str) or len(checksum)!=64 or any(c not in '0123456789abcdef' for c in checksum.lower()):
            raise ValueError('Missing SHA256 for '+f['path'])
        if not f.get('server_verified'): raise ValueError('Server evidence missing for '+f['path'])
        if not f.get('hf_revision'): raise ValueError('HF revision must be pinned')

def patch_api_workflow(api: dict, variant: str, poster_name: str='poster.jpg', seed: int=42, run_id: str='') -> dict:
    """Legacy A/B entry point, kept for historical regression tests.

    It builds the built-in fixture TaskSpec for the variant and
    delegates to patch_api_workflow_for_spec, so the patched workflow
    is byte-identical to the old hardcoded path. New code must use
    patch_api_workflow_for_spec with a TaskSpec instead.
    """
    from h3_task_spec import task_spec_for_variant

    spec = task_spec_for_variant(variant, "fixture", poster_name, seed=seed)
    return patch_api_workflow_for_spec(api, spec, poster_name, poster_name, run_id=run_id)


def patch_api_workflow_for_spec(api: dict, spec, ref_image_1_name: str = 'ref_image_1.jpg',
                                ref_image_2_name: str = 'ref_image_2.jpg', run_id: str = '') -> dict:
    """Patch known OFFICIAL GUI node IDs after ComfyUI's Save(API Format) export.
    We deliberately refuse if IDs/types differ; node drift must be explicitly mapped.

    `spec` is a TaskSpec (see h3_task_spec.py): node 137 <Picture 1>
    takes ref_image_1, node 139 <Picture 2> takes ref_image_2 (or
    ref_image_1 when the spec reuses it), node 136 takes the spec
    prompt and generation parameters, and the SaveVideo filename prefix
    carries the spec output_prefix + run_id. Nothing here is A/B
    specific anymore; the A/B values arrive as spec data.
    """
    if 'nodes' in api or not isinstance(api,dict):
        raise ValueError('Expected ComfyUI API format, not UI-format workflow (nodes/links)')
    need={'136':'MiniMaxH3ReferenceToVideo','137':'LoadImage','139':'LoadImage',
          '124':'BasicScheduler','129':'RandomNoise','92':'SaveVideo',
          '127':'UNETLoader','128':'CLIPLoader','146':'PrimitiveBoolean'}
    out=json.loads(json.dumps(api))
    for key,typ in need.items():
        node=out.get(key)
        if not isinstance(node,dict) or node.get('class_type')!=typ or not isinstance(node.get('inputs'),dict):
            raise ValueError(f'Official workflow API node contract mismatch: {key} expected {typ}')
    # Two references in the official template: node 137 is the main
    # (character/identity) reference, node 139 the scene/style auxiliary.
    out['137']['inputs']['image']=ref_image_1_name
    out['139']['inputs']['image']=ref_image_2_name
    node=out['136']['inputs']
    node['prompt']=spec.prompt
    node['width']=spec.width
    node['height']=spec.height
    node['length']=spec.animation_frames
    node['ref_image_size']='match'
    out['124']['inputs']['steps']=spec.steps
    out['124']['inputs']['scheduler']='beta'
    out['129']['inputs']['noise_seed']=spec.seed
    out['129']['inputs'].pop('control_after_generate',None)
    out['146']['inputs']['value']=False
    # Non-Turbo production: the Turbo LoRA branch is dead by construction
    # (node 146 forces the switch off), but the server validates the whole
    # reachable graph including the unselected branch. The Turbo LoRA file
    # (minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors, 1.82 GB)
    # is NOT in the 4-file depot, so its lora_name combo is absent from the
    # live file list and /prompt rejects the submission with HTTP 400
    # (2026-11-12 run h3-20261112T062103Z-901bc6). Remove the dead loader
    # and point both switch branches at the plain model; the official
    # workflow_api.json on disk is untouched, and Turbo can be re-enabled
    # later by seeding the LoRA instead of stripping it.
    if '145' in out and out['145'].get('class_type')=='LoraLoaderModelOnly':
        del out['145']
    if '141' in out and out['141'].get('class_type')=='ComfySwitchNode':
        out['141']['inputs']['on_true']=['127',0]
    # SaveVideo filename carries output_prefix + RUN_ID, so history/rescue can
    # prove a file belongs to this run (TASK-002 §4.2). Without a run_id
    # the legacy prefix is kept (existing tests / offline callers).
    out['92']['inputs']['filename_prefix'] = f'muse_h3_{spec.output_prefix}_{run_id}' if run_id else 'muse_h3_'+spec.output_prefix
    out['127']['inputs']['unet_name']='minimax_h3_ref2va_pruned_int8_convrot.safetensors'
    out['128']['inputs']['clip_name']='qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
    # API export can connect these via Primitive* nodes; replace links, not node IDs.
    if '138' in out and out['138'].get('class_type')=='PrimitiveStringMultiline':
        out['138']['inputs']['value']=spec.prompt
    if '143' in out and out['143'].get('class_type')=='PrimitiveInt':
        out['143']['inputs']['value']=spec.steps
    if '132' in out and out['132'].get('class_type')=='PrimitiveFloat':
        out['132']['inputs']['value']=spec.animation_frames/spec.fps
    # If a grouped node uses linked inputs, enforce literal overrides explicitly.
    for key in ('width','height','length','prompt'):
        if isinstance(node[key],list): raise ValueError('Unresolved reference to '+key)
    if spec.animation_frames % 17 != 5: raise ValueError('MiniMax H3 frame rule violated')
    return out
