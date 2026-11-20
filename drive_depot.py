"""Muse-host Google Drive Model Depot for MiniMax-H3 Ref2VA.

Default is read-only. The only write actions require explicit flags. No Colab
GPU is created here — except `remote-seed` (v0.2.0 F5), which launches ONE
temporary Colab session and is triple-gated (--allow-drive-write,
--allow-large-transfer, --confirm-remote-seed); it never runs without
those flags and always stops + verifies the session in `finally`.
An ADC user credential with Drive scopes is required.

Typical use:
    python drive_depot.py account-check --colab-auth adc
    python drive_depot.py init --allow-drive-write --colab-auth adc
    python drive_depot.py seed --allow-drive-write --allow-large-transfer --hf-revision <40-hex>
    python drive_depot.py verify --colab-auth adc

Uses *application-owned* Drive folders (drive.file permission). It deliberately
DOES NOT attempt to read the historic other-app H3 cache, which may be invisible
under drive.file scope even for the same human Google account.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, uuid
from auth_support import resolve_colab_executable
from pathlib import Path
from h3_contract import MODEL_FILES, sha256_file
from drive_api import file_digests, get_file_evidence, verify_uploaded_file

ROOT=Path(__file__).resolve().parent
DEPOT_DIR=ROOT/'model_cache'
CFG=DEPOT_DIR/'depot_config.json'
READY=DEPOT_DIR/'verified_manifest.json'
STATE=DEPOT_DIR/'depot_state.json'
HF_REPO='Comfy-Org/MiniMax-H3'
HF_RESOLVE_BASE='https://huggingface.co'
PIN_FILE=DEPOT_DIR/'pinned_hf_files.json'
DIRECT_CHUNK=1024*1024  # 1 MiB streaming writes
DIRECT_MAX_ATTEMPTS=3
DIRECT_CONNECT_TIMEOUT=30  # seconds
DIRECT_READ_TIMEOUT=120  # seconds between bytes; a slow-but-alive download is not killed
DIRECT_BACKOFF_SEC=(2,4)
# User has explicitly approved full Drive scope for this dedicated project OAuth client.
# The code itself still writes ONLY under the chosen depot folder.
DRIVE_SCOPE='https://www.googleapis.com/auth/drive'
CHUNK=8*1024*1024
REQUIRED=tuple(sorted(MODEL_FILES))
FOLDER_MIME='application/vnd.google-apps.folder'
# Historical Drive file IDs from 2026-11-09 H3 report. A 404 means
# inaccessible, NOT proven deleted. Reuse ONLY after checking HF/local hash.
LEGACY_VAE_IDS={
    'vae/minimax_h3_video_vae_int8_convrot.safetensors':'1j40DmcsWEmyHFjRiTjYYOIhZAuTX-iOA',
    'vae/minimax_h3_audio_vae_fp32.safetensors':'1NMhFPyq7qVSmZWJZg506LArud39lMTCF',
}


def atomic_json(path:Path,obj:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False,sort_keys=True,indent=2)
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)


def read_json(path:Path)->dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _is_transient_drive_error(exc: BaseException) -> bool:
    """Transient transport failures are retryable; verification failures are not.
    The VM egresses via a TLS-intercepting proxy; the TLS connection is
    intermittently torn down mid-request (ssl.SSLEOFError: EOF occurred in
    violation of protocol) during Drive API calls. The Drive calls are
    idempotent (reuse existing files, resume uploads, re-verify), so a retry
    with a fresh transport is safe. Checksum mismatches, duplicates and
    wrong-parent errors must still fail immediately.
    """
    import http.client, ssl
    return isinstance(exc, (ssl.SSLError, ConnectionError,
                            http.client.HTTPException, TimeoutError))


def drive_service():
    # Google Cloud ADC created by USER, not Colab CLI's private OAuth keyring.
    # User chose broad Drive scope; never hand the long-lived ADC file to Colab.
    # Transport (TASK-005 PR-7, Issue #30): requests via h3_drive_transport.
    # The stock httplib2 transport failed on this VM with
    # SSL WRONG_VERSION_NUMBER on every Drive API call (measured §6/§7:
    # same ADC, same scopes — requests files.get OK, httplib2 fails), and
    # build(credentials=...) does NOT escape httplib2 (google-api-python-
    # client 2.202.0 routes credentials through AuthorizedHttp). The
    # requests adapter is the only transport that works here. No request,
    # scope or verification logic is changed by this.
    import google.auth
    from h3_drive_transport import build_requests_drive_service

    creds,_=google.auth.default(scopes=[DRIVE_SCOPE,'https://www.googleapis.com/auth/userinfo.email'])
    return build_requests_drive_service(creds)


def account_matches(service, colab_auth:str)->bool:
    """Compare accounts in memory without emitting email or tokens to logs."""
    exe=resolve_colab_executable()
    proc=subprocess.run([exe,'--auth='+colab_auth,'whoami'],input='',capture_output=True,
                        text=True,timeout=65,check=False)
    # Hidden official developer command; fail closed if unavailable.
    if proc.returncode:raise RuntimeError('CLI whoami unavailable or not authorized: check CLI version and interactive OAuth')
    mat=re.search(r'^Email:\s*([^\s]+@[^\s]+)\s*$',proc.stdout,re.I|re.M)
    if not mat:raise RuntimeError('CLI whoami output lacks email; refuse account assertion')
    about=service.about().get(fields='user(emailAddress)').execute()
    drive_email=(about.get('user') or {}).get('emailAddress','').strip().casefold()
    cli_email=mat.group(1).strip().casefold()
    if not drive_email:raise RuntimeError('Drive API did not disclose signed-in user')
    return drive_email==cli_email


def list_named(service,parent:str,name:str):
    safe=name.replace('\\','\\\\').replace("'","\\'")
    q=f"'{parent}' in parents and name = '{safe}' and trashed = false"
    result=service.files().list(q=q,spaces='drive',pageSize=100,
         fields='nextPageToken,files(id,name,mimeType,size,md5Checksum,parents,appProperties)',
         supportsAllDrives=True,includeItemsFromAllDrives=True).execute()
    if result.get('nextPageToken'):raise RuntimeError('Ambiguous Drive listing, >100 items; refuse overwrite')
    return result.get('files',[])


def ensure_folder(service,parent:str|None,name:str,allow_write:bool):
    # Prevent duplicate root folders if VM crashed after remote creation but
    # before local depot_config.json was written.
    lookup_parent=parent or 'root'
    found=[f for f in list_named(service,lookup_parent,name) if f.get('mimeType')==FOLDER_MIME]
    if len(found)>1:raise RuntimeError('Duplicate folder name; refuse ambiguity')
    if found:return found[0]['id']
    if not allow_write:raise RuntimeError('Missing Drive folder '+name)
    body={'name':name,'mimeType':FOLDER_MIME}
    if parent:body['parents']=[parent]
    out=service.files().create(body=body,fields='id,name,mimeType',supportsAllDrives=True).execute()
    return out['id']


def verify_drive_entry(service,record:dict)->dict:
    """Refetch provider metadata, not a FUSE write acknowledgment or READY string."""
    meta=get_file_evidence(service,record['drive_file_id'])
    if meta.get('trashed') or meta.get('mimeType')==FOLDER_MIME:raise RuntimeError('Trashed/folder entry')
    if int(meta.get('size',-1))!=int(record['size']):raise RuntimeError('Server size mismatch for '+record['path'])
    expected_md5=record.get('server_md5','').lower()
    if len(expected_md5)!=32 or meta.get('md5Checksum','').lower()!=expected_md5:
        raise RuntimeError('Drive service MD5 mismatch for '+record['path'])
    if record.get('parent_id') not in meta.get('parents',[]):
        raise RuntimeError('Parent folder mismatch for '+record['path'])
    sha=record.get('sha256','')
    if len(sha)!=64 or not re.fullmatch('[0-9a-f]{64}',sha,re.I):
        raise RuntimeError('Missing local/HF SHA-256 evidence for '+record['path'])
    if meta.get('sha256Checksum') and meta['sha256Checksum'].lower()!=sha.lower():
        raise RuntimeError('Drive service SHA-256 mismatch for '+record['path'])
    return {'size':int(meta['size']),'file_id':meta['id'],'server_md5':meta['md5Checksum'],
            'sha256_source':'local_file_and_pinned_HF','server_sha256_available':bool(meta.get('sha256Checksum'))}


def verify_ready(service, account_ok:bool,ready_path:Path=READY)->dict:
    if not account_ok:raise RuntimeError('Colab account != Drive account')
    manifest=read_json(ready_path)
    if manifest.get('status')!='READY' or manifest.get('verification_method')!='drive_v3_server_api':
        raise RuntimeError('Missing/invalid verified manifest; ignore historical CACHE_READY.json')
    files=manifest.get('files',[])
    if {x.get('path') for x in files}!=set(REQUIRED) or len(files)!=4:
        raise RuntimeError('Four model files required, no duplicates')
    root_id=read_json(CFG)['drive_root_folder_id']
    if manifest.get('depot_root_id')!=root_id:raise RuntimeError('Wrong depot folder')
    # Verify provider-side READY payload, not just a persisted local flag.
    remote_ready=read_json(CFG).get('remote_manifest')
    if not isinstance(remote_ready,dict) or not remote_ready.get('drive_file_id'):
        raise RuntimeError('No server-verified READY document on Drive')
    proof=get_file_evidence(service,remote_ready['drive_file_id'])
    if proof.get('trashed') or root_id not in proof.get('parents',[]):
        raise RuntimeError('Drive READY document missing/wrong parent')
    if int(proof.get('size',-1))!=ready_path.stat().st_size or int(proof.get('size',-1))!=remote_ready.get('size'):
        raise RuntimeError('Drive READY document size mismatch')
    ready_md5=hashlib.md5(ready_path.read_bytes()).hexdigest()
    if proof.get('md5Checksum')!=ready_md5 or ready_md5!=remote_ready.get('md5'):
        raise RuntimeError('Drive READY document checksum mismatch')
    for x in files:
        if x.get('hf_revision')!=manifest.get('hf_revision'):raise RuntimeError('Mixed HF revision')
        verify_drive_entry(service,x)
    return {'ready':True,'files':len(files),'bytes':sum(x['size'] for x in files),
            'account_verified':True,'server_metadata_rechecked':True}


def upload_one(service,source:Path,folder_id:str,name:str,rel:str,sha:str,hf_rev:str)->dict:
    """Upload atomically from the Drive API's perspective; never overwrite wrong data."""
    local=file_digests(source)
    existing=list_named(service,folder_id,name)
    if len(existing)>1:raise RuntimeError('Duplicate Drive filenames for '+rel)
    if existing:
        meta=get_file_evidence(service,existing[0]['id'])
        if int(meta.get('size',-1))!=local['size'] or meta.get('md5Checksum','')!=local['md5']:
            raise RuntimeError('Existing Drive file differs; DO NOT overwrite: '+rel)
        file_id=meta['id']
    else:
        from google.auth.transport.requests import AuthorizedSession
        import google.auth
        from drive_resumable import upload_with_persistent_session
        creds,_=google.auth.default(scopes=[DRIVE_SCOPE])
        auth_session=AuthorizedSession(creds)
        session_key=hashlib.sha256((folder_id+'/'+name+'/'+hf_rev).encode()).hexdigest()[:32]
        sidecar=DEPOT_DIR/'.upload_sessions'/(session_key+'.json')
        response=upload_with_persistent_session(auth_session,source,name,folder_id,rel,hf_rev,sidecar,sha)
        file_id=response['id']
    proof=verify_uploaded_file(service,file_id,source)
    entry={'path':rel,'drive_file_id':file_id,'parent_id':folder_id,
           'size':local['size'],'sha256':sha,'server_md5':proof['server_md5'],
           'server_verified':True,'hf_revision':hf_rev}
    verify_drive_entry(service,entry)
    return entry



def publish_manifest_to_drive(service,root_id:str,manifest:dict)->dict:
    """Durably publish a versioned READY file *on Drive* after four-file checks.

    A local READY is never written before provider-side metadata for this
    exact JSON has been checked. This is an auditable small final commit.
    """
    from googleapiclient.http import MediaFileUpload
    revision=manifest['hf_revision']
    name='READY_'+revision+'.json'
    local=DEPOT_DIR/name
    atomic_json(local,manifest)
    existing=list_named(service,root_id,name)
    if len(existing)>1:raise RuntimeError('Multiple Drive READY documents')
    if existing:
        proof=verify_uploaded_file(service,existing[0]['id'],local)
        file_id=existing[0]['id']
    else:
        media=MediaFileUpload(str(local),mimetype='application/json',resumable=False)
        obj=service.files().create(body={'name':name,'parents':[root_id]},media_body=media,
            fields='id,name,size,md5Checksum,parents',supportsAllDrives=True).execute()
        file_id=obj['id'];proof=verify_uploaded_file(service,file_id,local)
    server=get_file_evidence(service,file_id)
    if root_id not in server.get('parents',[]):
        raise RuntimeError('READY document not in model depot root')
    return {'drive_file_id':file_id,'name':name,'md5':proof['server_md5'],
            'size':proof['size']}



def maybe_copy_verified_legacy_vae(service, source:Path, target_folder:str, name:str, rel:str)->bool:
    """Prefer server-side copy of a historic VAE ONLY when bytes match HF.

    This is an optimization, not trust in the old CACHE_READY.json. Full-Drive
    scope is necessary for access to historical other-app-created files.
    """
    legacy_id=LEGACY_VAE_IDS.get(rel)
    if not legacy_id or list_named(service,target_folder,name):return False
    try:
        old=get_file_evidence(service,legacy_id)
    except Exception as e:
        status=getattr(getattr(e,'resp',None),'status',None)
        if status in (403,404):
            print('LEGACY_VAE_INACCESSIBLE_NOT_MISSING',rel,flush=True)
            return False
        raise
    digest=file_digests(source)  # source already checked against pinned HF LFS
    if old.get('trashed') or old.get('name')!=name or int(old.get('size',-1))!=digest['size']:
        return False
    if old.get('md5Checksum','').lower()!=digest['md5'].lower():
        return False
    if old.get('sha256Checksum') and old['sha256Checksum'].lower()!=digest['sha256'].lower():
        return False
    res=service.files().copy(fileId=legacy_id,
        body={'name':name,'parents':[target_folder]},
        fields='id,name,size,md5Checksum,parents',supportsAllDrives=True).execute()
    verify_uploaded_file(service,res['id'],source)
    if target_folder not in get_file_evidence(service,res['id']).get('parents',[]):
        raise RuntimeError('Copied historic VAE into wrong depot folder')
    print('LEGACY_VAE_SERVER_COPIED_VERIFIED',rel,flush=True)
    return True


def fetch_pinned_metadata(revision:str)->dict:
    """One HF API call to pin sizes + official LFS SHA-256 for this revision.

    The result is written to PIN_FILE so every later direct download uses
    pinned values and needs no further HF API call. Never prints URLs.
    """
    from huggingface_hub import HfApi
    if not re.fullmatch('[0-9a-f]{40}',revision):
        raise ValueError('HF revision must be a pinned 40-char commit SHA')
    info=HfApi().model_info(repo_id=HF_REPO,revision=revision,files_metadata=True)
    if info.sha!=revision:raise RuntimeError('Resolved HF revision differs')
    siblings={x.rfilename:x for x in info.siblings}
    missing=set(REQUIRED)-set(siblings)
    if missing:raise RuntimeError('HF files missing: '+repr(sorted(missing)))
    files={}
    for rel in sorted(REQUIRED):
        sib=siblings[rel]
        size=getattr(sib,'size',None)
        lfs=getattr(sib,'lfs',None)
        sha=(lfs or {}).get('sha256') if isinstance(lfs,dict) else getattr(lfs,'sha256',None)
        if not size or not sha or len(str(sha))!=64:
            raise RuntimeError('Pinned HF metadata incomplete for '+rel)
        files[rel]={'size':int(size),'sha256':str(sha).lower(),'lfs':bool(lfs)}
    others=[]
    for rel in sorted(siblings):
        if rel in files:continue
        sib=siblings[rel]
        lfs=getattr(sib,'lfs',None)
        lfs_sha=(lfs or {}).get('sha256') if isinstance(lfs,dict) else getattr(lfs,'sha256',None)
        blob_sha=getattr(sib,'sha256',None)
        sha=str(lfs_sha or blob_sha or '').lower()
        others.append({'path':rel,'size':int(getattr(sib,'size',0) or 0),
                       'sha256':sha,'lfs':bool(lfs)})
    pin={'repo':HF_REPO,'revision':revision,'files':files,'other_files':others}
    atomic_json(PIN_FILE,pin)
    return pin


def load_pinned_metadata(revision:str)->dict:
    """Reuse a previously fetched pin; fetch once (one HF API call) if absent."""
    if PIN_FILE.exists():
        pin=read_json(PIN_FILE)
        if pin.get('repo')==HF_REPO and pin.get('revision')==revision and set(pin.get('files',{}))==set(REQUIRED):
            return pin
    return fetch_pinned_metadata(revision)


def download_pinned_model(rel:str,expected_size:int,expected_sha256:str,dest_dir:Path,revision:str)->Path:
    """Direct sequential download of one pinned model file. No concurrency.

    Streaming write to `<file>.part` with HTTP Range resume, bounded retries
    and connect/read timeouts. Verifies exact byte count + official LFS
    SHA-256 before the file is ever handed to Drive upload; a mismatch
    deletes the partial file and raises. Never prints URLs (a CDN redirect
    may carry a signature) or credentials. A `.part` file is never READY.
    """
    import requests
    if not re.fullmatch('[0-9a-f]{64}',expected_sha256 or '',re.I):
        raise ValueError('Pinned SHA-256 required for '+rel)
    if not expected_size or expected_size<=0:
        raise ValueError('Pinned byte count required for '+rel)
    dest_dir.mkdir(parents=True,exist_ok=True)
    dest=dest_dir/rel
    dest.parent.mkdir(parents=True,exist_ok=True)
    part=Path(str(dest)+'.part')
    url=f'{HF_RESOLVE_BASE}/{HF_REPO}/resolve/{revision}/{rel}'
    session=requests.Session()
    # Reuse a complete, already-verified local copy instead of re-downloading.
    if dest.is_file() and dest.stat().st_size==expected_size and sha256_file(dest)==expected_sha256.lower():
        return dest
    if dest.is_file():
        dest.unlink()  # stale or corrupt local copy; never upload it
    start=part.stat().st_size if part.is_file() else 0
    if start>expected_size:
        part.unlink();start=0
    if start==expected_size and sha256_file(part)==expected_sha256.lower():
        os.replace(part,dest);return dest
    last_error=None
    for attempt in range(1,DIRECT_MAX_ATTEMPTS+1):
        headers={}
        if start>0:headers['Range']=f'bytes={start}-'
        try:
            with session.get(url,headers=headers,stream=True,
                             timeout=(DIRECT_CONNECT_TIMEOUT,DIRECT_READ_TIMEOUT),
                             allow_redirects=True) as resp:
                status=resp.status_code
                if status==416:
                    # Server says the range is unsatisfiable: the part may
                    # already be complete; verify before deciding.
                    if part.is_file() and part.stat().st_size==expected_size and sha256_file(part)==expected_sha256.lower():
                        os.replace(part,dest);return dest
                    part.unlink(missing_ok=True);start=0;last_error=RuntimeError('HTTP 416 with incomplete part for '+rel)
                    continue
                if status==200 and start>0:
                    # Server ignored Range; restart from zero, do not append.
                    part.unlink(missing_ok=True);start=0;last_error=RuntimeError('Server ignored Range for '+rel)
                    continue
                if status not in (200,206):
                    if status in (429,500,502,503,504):
                        last_error=RuntimeError('Direct download HTTP '+str(status)+' for '+rel)
                        if attempt<DIRECT_MAX_ATTEMPTS:time.sleep(DIRECT_BACKOFF_SEC[min(attempt-1,len(DIRECT_BACKOFF_SEC)-1)]);continue
                    raise RuntimeError('Direct download HTTP '+str(status)+' for '+rel)
                if status==206:
                    cr=resp.headers.get('Content-Range','')
                    m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',cr)
                    if not m or int(m.group(1))!=start or int(m.group(3))!=expected_size:
                        raise RuntimeError('Unexpected Content-Range for '+rel)
                mode='ab' if start>0 else 'wb'
                written=start
                with part.open(mode) as f:
                    for chunk in resp.iter_content(chunk_size=DIRECT_CHUNK):
                        if not chunk:continue
                        f.write(chunk);written+=len(chunk)
                        if written>expected_size:
                            raise RuntimeError('Direct download overran pinned size for '+rel)
                start=written
                if written==expected_size:break
                last_error=RuntimeError('Direct download short read for '+rel)
        except (RuntimeError,ValueError):
            raise
        except Exception as e:
            last_error=e
        if attempt<DIRECT_MAX_ATTEMPTS:time.sleep(DIRECT_BACKOFF_SEC[min(attempt-1,len(DIRECT_BACKOFF_SEC)-1)]);start=part.stat().st_size if part.is_file() else 0
    if part.is_file() and part.stat().st_size==expected_size and sha256_file(part)==expected_sha256.lower():
        os.replace(part,dest)
        print('DIRECT_DOWNLOAD_OK',rel,'bytes',expected_size,flush=True)
        return dest
    if part.is_file():part.unlink(missing_ok=True)
    raise RuntimeError('Direct download failed checksum/size for '+rel+': '+(str(last_error)[:120] if last_error else 'unknown'))


def seed(service,root_id:str,revision:str,source:str='hub'):
    if source not in ('hub','direct'):
        raise ValueError("seed source must be 'hub' or 'direct'")
    if not re.fullmatch('[0-9a-f]{40}',revision):
        raise ValueError('HF revision must be a pinned 40-char commit SHA')
    if source=='hub':
        from huggingface_hub import HfApi,hf_hub_download
        api=HfApi(); info=api.model_info(repo_id=HF_REPO,revision=revision,files_metadata=True)
        if info.sha!=revision:raise RuntimeError('Resolved HF revision differs')
        siblings={x.rfilename:x for x in info.siblings}
        missing=set(REQUIRED)-set(siblings)
        if missing:raise RuntimeError('HF files missing: '+repr(sorted(missing)))
        pin={}
        for rel in REQUIRED:
            lfs=getattr(siblings[rel],'lfs',None)
            hf_sha=(lfs or {}).get('sha256') if isinstance(lfs,dict) else getattr(lfs,'sha256',None)
            pin[rel]={'size':int(getattr(siblings[rel],'size',0) or 0),'sha256':str(hf_sha or '').lower()}
        def acquire(rel):
            return Path(hf_hub_download(repo_id=HF_REPO,filename=rel,revision=revision,
                                        local_dir=str(DEPOT_DIR/'hf_stage')))
    else:
        pin_data=load_pinned_metadata(revision)
        pin=pin_data['files']
        def acquire(rel):
            return download_pinned_model(rel,pin[rel]['size'],pin[rel]['sha256'],
                                         DEPOT_DIR/'hf_stage',revision)
    required_bytes=sum(pin[rel]['size'] for rel in REQUIRED)
    if required_bytes and shutil.disk_usage(DEPOT_DIR).free<required_bytes*1.3:
        raise RuntimeError('Insufficient Muse home storage; cannot stage all weights')
    entries=[]
    atomic_json(STATE,{'status':'UPLOADING','hf_revision':revision,'verified_files':[]})
    for rel in REQUIRED:
        entry = None
        for attempt in range(1, 4):
            try:
                sub, name = rel.split('/', 1)
                folder_id = ensure_folder(service, root_id, sub, allow_write=True)
                local = acquire(rel)
                sha = sha256_file(local)
                hf_sha = pin[rel]['sha256']
                if not hf_sha or sha.lower() != hf_sha.lower():
                    raise RuntimeError('Hugging Face LFS checksum mismatch for ' + rel)
                if rel in LEGACY_VAE_IDS:
                    try:
                        maybe_copy_verified_legacy_vae(service, local, folder_id, name, rel)
                    except Exception as e:
                        # The legacy copy is an optimization, not a requirement.
                        # If it fails transiently (egress TLS EOF), fall back to
                        # the normal resumable upload instead of killing the file.
                        # A copy that succeeded server-side but lost its response
                        # is still safe: upload_one reuses the existing file after
                        # verifying size + MD5. Non-transient errors still raise.
                        if not _is_transient_drive_error(e):
                            raise
                        print('LEGACY_COPY_UNAVAILABLE_FALLBACK_UPLOAD', rel,
                              type(e).__name__, flush=True)
                entry = upload_one(service, local, folder_id, name, rel, sha, revision)
                break
            except Exception as e:
                if not _is_transient_drive_error(e) or attempt >= 3:
                    raise
                print('TRANSIENT_DRIVE_ERROR_RETRY', rel, 'attempt', attempt,
                      type(e).__name__, flush=True)
                time.sleep(5 * attempt)
                service = drive_service()  # fresh transport; discard stale TLS connection
        entries.append(entry)
        atomic_json(STATE, {'status': 'VERIFYING', 'hf_revision': revision, 'verified_files': entries})
        print('FILE_VERIFIED', rel, 'size', entry['size'], '(no secret)', flush=True)
    return _commit_ready_manifest(service, root_id, revision, entries)


def _commit_ready_manifest(service, root_id: str, revision: str, entries: list) -> dict:
    """Shared final commit for seed() and remote_seed(): verify every
    entry against the Drive provider, publish the versioned READY
    document on Drive, then write the local READY + state. The manifest
    commit happens only AFTER all 4 remote provider checks. An entry
    becomes server_verified only once its provider check passed here
    (the local seed's entries arrive verified; the remote seed's arrive
    unverified and are marked here)."""
    manifest = {'status': 'READY', 'verification_method': 'drive_v3_server_api',
                'account_verified': True, 'verified_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'depot_root_id': root_id, 'hf_repo': HF_REPO, 'hf_revision': revision, 'files': entries}
    for attempt in range(1, 4):
        try:
            for e in entries: verify_drive_entry(service, e)
            for e in entries: e['server_verified'] = True
            remote_ready = publish_manifest_to_drive(service, root_id, manifest)
            break
        except Exception as e:
            if not _is_transient_drive_error(e) or attempt >= 3:
                raise
            print('TRANSIENT_DRIVE_ERROR_RETRY manifest attempt', attempt,
                  type(e).__name__, flush=True)
            time.sleep(5 * attempt)
            service = drive_service()
    config=read_json(CFG)
    config['remote_manifest']=remote_ready
    atomic_json(CFG,config)
    atomic_json(READY,manifest)
    atomic_json(STATE,{'status':'READY','hf_revision':revision,'verified_files':len(entries)})
    return {'ready':True,'files':len(entries),'bytes':sum(e['size'] for e in entries)}


# ---------------------------------------------------------------------------
# v0.2.0 F5: in-cloud remote seed.
#
# `remote-seed` downloads the locked pinned weights INSIDE a temporary
# Colab session (fast in-cloud egress to Drive) instead of staging 37 GB
# through this VM. It is triple-gated (--allow-drive-write,
# --allow-large-transfer, --confirm-remote-seed) and never runs live
# without separate user authorization. The session is watchdog-protected
# and is stopped + tristate-verified STOPPED in a finally on every path;
# an unconfirmed stop raises REMOTE_SEED_ORPHAN_RISK and the seed never
# reports success. The token value never appears in the generated script,
# in CLI arguments, or in logs: it travels as a 0600 file uploaded to the
# session, and the script reads it from disk.
# ---------------------------------------------------------------------------
REMOTE_SEED_SESSION_PREFIX = "muse-h3-seed-"
REMOTE_SEED_DIR = "/content/muse_h3"
REMOTE_SEED_DEADLINE_SEC = 3600
REMOTE_SEED_STOP_ATTEMPTS = 3
REMOTE_SEED_RANGES = 8


def _colab_run_cli(cmd, timeout=90):
    """Minimal colab-CLI wrapper (mirrors h3_controller.run_cli's binary
    lookup and timeout env, without importing the controller). The token
    is never a CLI argument, so stdout/stderr here carry no credentials;
    on failure only the stderr tail is surfaced."""
    exe = resolve_colab_executable()
    env = {**os.environ, "REQUEST_TIMEOUT": "120",
           "COLAB_CLI_REQUEST_TIMEOUT": "180"}
    proc = subprocess.run([exe, "--auth=adc", *cmd], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL,
                          timeout=timeout, check=False, env=env)
    if proc.returncode != 0:
        raise RuntimeError("CLI failed: " + str(cmd) + "\n" + proc.stderr[-1400:])
    return {"args": cmd, "returncode": proc.returncode,
            "stdout": proc.stdout, "stderr": proc.stderr}


REMOTE_SEED_SCRIPT_TEMPLATE = """import concurrent.futures, hashlib, json, os, sys, urllib.request

TOKEN_PATH = "/content/muse_h3/seed_token.json"
REPORT_PATH = "/content/muse_h3/seed_report.json"
STAGE_DIR = "/content/seed_stage"
PAYLOAD = __PAYLOAD_JSON__


def _digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_range(url, start, end):
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (start, end)})
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status != 206:
                    raise RuntimeError("expected 206, got %d" % resp.status)
                return resp.read()
        except Exception:
            if attempt == 3:
                raise
            import time as _time
            _time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def download_one(entry):
    url = "%s/%s/resolve/%s/%s" % (PAYLOAD["hf_resolve_base"], PAYLOAD["hf_repo"],
                                  PAYLOAD["revision"], entry["rel"])
    dest = os.path.join(STAGE_DIR, entry["rel"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    size = entry["size"]
    bounds = []
    step = (size + 8 - 1) // 8
    start = 0
    while start < size:
        bounds.append((start, min(start + step, size) - 1))
        start = bounds[-1][1] + 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        parts = list(pool.map(lambda b: _fetch_range(url, b[0], b[1]), bounds))
    with open(dest, "wb") as f:
        for part in parts:
            f.write(part)
    if os.path.getsize(dest) != size:
        raise RuntimeError("size mismatch after download: " + entry["rel"])
    if _digest(dest, "sha256") != entry["sha256"].lower():
        raise RuntimeError("SHA-256 mismatch after download: " + entry["rel"])
    return dest


def upload_one(service, entry, local_path):
    # Reuse an identical Drive file (a re-run must never overwrite a
    # differing file, and must never re-upload identical bytes).
    name = os.path.basename(entry["rel"])
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = "'%s' in parents and name = '%s' and trashed = false" % (entry["folder_id"], safe)
    found = service.files().list(q=q, spaces="drive", pageSize=100,
                                 fields="files(id,name,size,md5Checksum)",
                                 supportsAllDrives=True,
                                 includeItemsFromAllDrives=True).execute().get("files", [])
    if len(found) > 1:
        raise RuntimeError("duplicate Drive file for " + entry["rel"])
    local_md5 = _digest(local_path, "md5")
    if found:
        meta = found[0]
        if int(meta.get("size", -1)) != entry["size"] or (meta.get("md5Checksum") or "").lower() != local_md5:
            raise RuntimeError("existing Drive file differs; DO NOT overwrite: " + entry["rel"])
        return {"path": entry["rel"], "drive_file_id": meta["id"],
                "parent_id": entry["folder_id"], "size": entry["size"],
                "sha256": entry["sha256"], "server_md5": meta["md5Checksum"],
                "server_verified": False, "hf_revision": PAYLOAD["revision"]}
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(local_path, resumable=True, chunksize=8 * 1024 * 1024)
    req = service.files().create(body={"name": name, "parents": [entry["folder_id"]]},
                                 media_body=media,
                                 fields="id,name,size,md5Checksum,parents",
                                 supportsAllDrives=True)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk()
    if resp.get("md5Checksum", "").lower() != local_md5:
        raise RuntimeError("Drive MD5 mismatch after upload: " + entry["rel"])
    return {"path": entry["rel"], "drive_file_id": resp["id"],
            "parent_id": entry["folder_id"], "size": entry["size"],
            "sha256": entry["sha256"], "server_md5": resp["md5Checksum"],
            "server_verified": False, "hf_revision": PAYLOAD["revision"]}


def main():
    token = json.load(open(TOKEN_PATH))["access_token"]
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    service = build("drive", "v3", credentials=Credentials(token))
    os.makedirs(STAGE_DIR, exist_ok=True)
    entries = []
    for entry in PAYLOAD["files"]:
        local = download_one(entry)
        entries.append(upload_one(service, entry, local))
        os.remove(local)
    report = {"seeded": True, "files": entries}
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f)
    print("SEED_REPORT_JSON", json.dumps(report), flush=True)


main()
"""


def _remote_seed_script(pin: dict, folder_ids: dict, revision: str) -> str:
    """Generate the self-contained script the seed session runs. The
    pinned files (rel, size, SHA-256, depot folder id) are embedded as
    JSON; the token is NOT in the script — the script reads it from the
    uploaded 0600 token file at runtime."""
    payload = {
        "hf_resolve_base": HF_RESOLVE_BASE,
        "hf_repo": HF_REPO,
        "revision": revision,
        "files": [{"rel": rel, "size": int(pin[rel]["size"]),
                   "sha256": str(pin[rel]["sha256"]).lower(),
                   "folder_id": folder_ids[rel.split("/", 1)[0]]}
                  for rel in REQUIRED],
    }
    return REMOTE_SEED_SCRIPT_TEMPLATE.replace(
        "__PAYLOAD_JSON__", json.dumps(payload, ensure_ascii=False))


def parse_seed_report(text: str) -> dict:
    """Extract the remote session's SEED_REPORT_JSON from exec output.
    The last such line wins. Returns the report dict; raises when no
    line exists or the JSON is malformed — a missing report is never
    treated as a successful seed."""
    report = None
    for line in (text or "").splitlines():
        if line.startswith("SEED_REPORT_JSON"):
            report = json.loads(line.split("SEED_REPORT_JSON", 1)[1].strip())
    if not isinstance(report, dict):
        raise RuntimeError("Remote seed session printed no SEED_REPORT_JSON")
    return report


def _validate_seed_report(report: dict, pin: dict, revision: str) -> list:
    """Check the remote report against the pinned metadata before any
    Drive verification. Returns the entries in REQUIRED order. Raises
    on any mismatch — the local side never trusts the remote claim."""
    if not report.get("seeded"):
        raise RuntimeError("Remote seed report does not claim seeded=true")
    by_rel = {}
    for entry in report.get("files") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise RuntimeError("Remote seed report has a malformed entry")
        if entry["path"] in by_rel:
            raise RuntimeError("Remote seed report duplicates " + entry["path"])
        by_rel[entry["path"]] = entry
    entries = []
    for rel in REQUIRED:
        entry = by_rel.get(rel)
        if entry is None:
            raise RuntimeError("Remote seed report is missing " + rel)
        if int(entry.get("size", -1)) != int(pin[rel]["size"]):
            raise RuntimeError("Remote seed size mismatch for " + rel)
        if str(entry.get("sha256", "")).lower() != str(pin[rel]["sha256"]).lower():
            raise RuntimeError("Remote seed SHA-256 mismatch for " + rel)
        if entry.get("hf_revision") != revision:
            raise RuntimeError("Remote seed HF revision mismatch for " + rel)
        if not entry.get("drive_file_id") or not entry.get("server_md5"):
            raise RuntimeError("Remote seed entry lacks Drive evidence for " + rel)
        entries.append(entry)
    return entries


class RemoteSeedRunner:
    """The real session runner: the colab CLI + the watchdog's managed
    session records. All methods are small and independently replaceable
    by a fake in tests (the runner is the only part that touches a real
    Colab session)."""

    def __init__(self, work_dir: Path, deadline_sec: int = REMOTE_SEED_DEADLINE_SEC):
        self.work_dir = Path(work_dir)
        self.deadline_sec = deadline_sec
        self.task_id = "remote-seed"

    def create(self, session: str) -> None:
        from h3_budget import allocation_command
        from h3_session_watchdog import (confirm_session_active,
                                         query_session_state, record_session)
        deadline = (dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(seconds=self.deadline_sec)).isoformat()
        record_session(session, "G4", self.task_id, deadline,
                       production_task_id=self.task_id, status="allocating")
        try:
            _colab_run_cli(allocation_command(
                session, "G4", allow_g4=True, allow_a100_highmem=False),
                timeout=420)
        except Exception:
            # A failed `new` must not leave a late allocation behind:
            # if the session did appear, stop it here before raising.
            try:
                from h3_session_watchdog import SessionState
                if query_session_state(session) is SessionState.ACTIVE:
                    self.stop_and_verify(session)
            except Exception:
                pass
            raise
        if confirm_session_active(session) is None:
            raise RuntimeError(
                "seed session record vanished after allocation; refusing "
                "to proceed with an unregistered session")
        self._start_watchdog(session)

    def _start_watchdog(self, session: str) -> None:
        ready_path = self.work_dir / f"watchdog_{session}_ready.json"
        log_path = self.work_dir / f"watchdog_{session}.log"
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "h3_session_watchdog.py"), "watch",
             "--session", session, "--production-task-id", self.task_id,
             "--ready-file", str(ready_path)],
            stdout=open(log_path, "a"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if ready_path.exists():
                return
            if proc.poll() is not None:
                raise RuntimeError("WATCHDOG_NOT_RUNNING: seed watchdog exited early")
            time.sleep(2)
        proc.terminate()
        raise RuntimeError("WATCHDOG_NOT_RUNNING: seed watchdog did not become ready")

    def upload(self, session: str, local_path: Path, remote_name: str) -> None:
        _colab_run_cli(["upload", "-s", session, str(local_path),
                        REMOTE_SEED_DIR + "/" + remote_name], timeout=300)

    def exec_script(self, session: str, remote_path: str) -> dict:
        return _colab_run_cli(["exec", "-s", session, "-f", remote_path,
                               "--timeout", "600"], timeout=self.deadline_sec)

    def stop_and_verify(self, session: str) -> dict:
        """Tristate stop (mirrors h3_controller.stop_and_verify_session):
        STOPPED only from a query that says the session is gone; ACTIVE
        retries the stop; UNKNOWN never counts as stopped. Marks the
        managed record stopped only on a confirmed STOPPED verdict."""
        from h3_session_watchdog import (SessionState, mark_session_stopped,
                                         query_session_state)
        stopped = False
        last_state = SessionState.UNKNOWN
        for _attempt in range(REMOTE_SEED_STOP_ATTEMPTS):
            try:
                state = query_session_state(session)
                last_state = state
                if state is SessionState.STOPPED:
                    stopped = True
                    break
                if state is SessionState.ACTIVE:
                    _colab_run_cli(["stop", "-s", session], timeout=65)
                    state = query_session_state(session)
                    last_state = state
                    if state is SessionState.STOPPED:
                        stopped = True
                        break
            except Exception:
                pass
            time.sleep(3)
        if stopped:
            try:
                mark_session_stopped(session, SessionState.STOPPED)
            except Exception:
                pass
        return {"session": session, "stopped_confirmed": stopped,
                "state": last_state.value}


def remote_seed(service, root_id: str, revision: str, runner=None,
                token_fn=None, deadline_sec: int = REMOTE_SEED_DEADLINE_SEC,
                work_dir: Path | None = None) -> dict:
    """Seed the Drive depot from inside a temporary Colab session.

    Triple-gated by the CLI (--allow-drive-write, --allow-large-transfer,
    --confirm-remote-seed); this function itself assumes the caller
    checked the gates. The session is stopped + tristate-verified in a
    finally on every path, and the seed only reports success when the
    stop is confirmed STOPPED. Raises RuntimeError on any failure.
    Never runs a live session in tests: pass a fake `runner` and
    `token_fn`."""
    if not re.fullmatch("[0-9a-f]{40}", revision or ""):
        raise ValueError("HF revision must be a pinned 40-char commit SHA")
    pin_data = load_pinned_metadata(revision)
    pin = pin_data["files"]
    missing = [rel for rel in REQUIRED
               if rel not in pin or not pin[rel].get("size") or not pin[rel].get("sha256")]
    if missing:
        raise RuntimeError("Pinned HF metadata incomplete for " + repr(sorted(missing)))
    folder_ids = {}
    for rel in REQUIRED:
        sub = rel.split("/", 1)[0]
        if sub not in folder_ids:
            folder_ids[sub] = ensure_folder(service, root_id, sub, allow_write=True)
    token_fn = token_fn or _mint_seed_token
    token_doc = token_fn()
    if not isinstance(token_doc, dict) or not token_doc.get("access_token"):
        raise RuntimeError("Seed token minting did not return an access token")
    started = time.monotonic()
    work = Path(work_dir) if work_dir is not None else Path(
        tempfile.mkdtemp(prefix="muse-remote-seed-"))
    work.mkdir(parents=True, exist_ok=True)
    from h3_drive_token import write_token_file
    token_path = write_token_file(work / "seed_token.json", token_doc)
    script_path = work / "remote_seed.py"
    script = _remote_seed_script(pin, folder_ids, revision)
    # Defense in depth: the token value must never be in the script.
    if token_doc["access_token"] in script:
        raise RuntimeError("Seed token value leaked into the generated script; refusing")
    script_path.write_text(script, encoding="utf-8")
    session = REMOTE_SEED_SESSION_PREFIX + uuid.uuid4().hex[:10]
    runner = runner or RemoteSeedRunner(work, deadline_sec=deadline_sec)
    body_error = None
    result = None
    created = False
    try:
        runner.create(session)
        created = True
        runner.upload(session, token_path, "seed_token.json")
        runner.upload(session, script_path, "remote_seed.py")
        exec_result = runner.exec_script(session, REMOTE_SEED_DIR + "/remote_seed.py")
        report = parse_seed_report((exec_result or {}).get("stdout", ""))
        entries = _validate_seed_report(report, pin, revision)
        committed = _commit_ready_manifest(service, root_id, revision, entries)
        result = {"remote_seeded": True, "files": committed["files"],
                  "bytes": committed["bytes"], "session": session,
                  "seed_seconds": round(time.monotonic() - started, 3)}
    except Exception as e:  # noqa: BLE001 — the stop still runs below
        body_error = e
    finally:
        if created:
            stop = runner.stop_and_verify(session)
            if not stop.get("stopped_confirmed"):
                raise RuntimeError(
                    "REMOTE_SEED_ORPHAN_RISK: seed session " + session +
                    " could not be confirmed STOPPED after "
                    + str(REMOTE_SEED_STOP_ATTEMPTS) +
                    " attempts; reconcile it manually (colab sessions / "
                    "stop) before assuming the GPU is released"
                    + (f"; seed body also failed: {body_error}" if body_error else ""))
    if body_error is not None:
        raise body_error
    # The finally above raised unless the stop was confirmed STOPPED,
    # so reaching here is the proof the session is released.
    result["stopped_confirmed"] = True
    return result


def _mint_seed_token() -> dict:
    """Mint the short-lived drive.file token for the seed session (lazy
    import: drive_depot stays importable without google libs)."""
    from h3_drive_token import mint_file_token
    return mint_file_token()


def smoke(service,root_id:str)->dict:
    """Idempotent, real Drive API upload/readback canary, zero Colab GPU."""
    folder_id=ensure_folder(service,root_id,'_checks',allow_write=True)
    source=DEPOT_DIR/'smoke_test.bin'
    source.parent.mkdir(parents=True,exist_ok=True)
    payload=b'Muse H3 Drive depot canary v1\n'
    if source.exists() and source.read_bytes()!=payload:
        raise RuntimeError('Unexpected existing smoke source; do not overwrite')
    if not source.exists():source.write_bytes(payload)
    expected_sha=sha256_file(source)
    entry=upload_one(service,source,folder_id,'smoke_test.bin',
                     '_checks/smoke_test.bin',expected_sha,'0'*40)
    verify_drive_entry(service,entry)
    return {'drive_canary_verified':True,'file_id':entry['drive_file_id'],
            'size':entry['size'],'server_md5_verified':True,'no_gpu_created':True}


def cli():
    p=argparse.ArgumentParser(description='Muse independent Drive H3 model depot; ZERO Colab GPU allocations '
                                            '(except remote-seed, which launches one temporary session)')
    p.add_argument('action',choices=('account-check','init','smoke','seed','remote-seed','verify'))
    p.add_argument('--colab-auth',choices=('oauth2','adc'),default='oauth2')
    p.add_argument('--allow-drive-write',action='store_true')
    p.add_argument('--allow-large-transfer',action='store_true')
    p.add_argument('--confirm-remote-seed',action='store_true',
                   help='v0.2.0 F5: acknowledge that remote-seed launches a real temporary Colab session')
    p.add_argument('--hf-revision',default='')
    p.add_argument('--source',choices=('hub','direct'),default='hub',
                   help='model download strategy: hub (huggingface_hub) or direct (pinned resolve URL + Range resume)')
    a=p.parse_args()
    if a.action in ('init','smoke','seed','remote-seed') and not a.allow_drive_write:
        p.error('Mutating Drive requires --allow-drive-write')
    if a.action in ('seed','remote-seed') and not a.allow_large_transfer:
        p.error('37GB-class transfer requires --allow-large-transfer')
    if a.action=='remote-seed' and not a.confirm_remote_seed:
        p.error('Remote seed launches a real temporary Colab session; '
                'requires --confirm-remote-seed')
    try:
        s=drive_service()
        ok=account_matches(s,a.colab_auth)
        if not ok:raise RuntimeError('BLOCKED: Drive / Colab Google account mismatch')
        if a.action=='account-check':result={'same_google_account':True,'no_gpu_created':True}
        elif a.action=='init':
            if CFG.exists():raise RuntimeError('Depot already initialized; refuse duplicate creation')
            folder=ensure_folder(s,None,'Muse-H3-Model-Depot',allow_write=True)
            atomic_json(CFG,{'drive_root_folder_id':folder,'scope':'drive','hf_repo':HF_REPO})
            result={'created_new_app_folder':True,'root_id':folder,'no_gpu_created':True}
        elif a.action=='smoke':
            config=read_json(CFG)
            result=smoke(s,config['drive_root_folder_id'])
        elif a.action=='seed':
            config=read_json(CFG)
            result=seed(s,config['drive_root_folder_id'],a.hf_revision,source=a.source)
        elif a.action=='remote-seed':
            config=read_json(CFG)
            result=remote_seed(s,config['drive_root_folder_id'],a.hf_revision)
        else:result=verify_ready(s,ok)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as e:
        # Never log Google OAuth tokens, URLs or raw Google API exceptions:
        # they may contain authorization data in unusual errors.
        print('BLOCKED:',type(e).__name__,str(e)[:180] if isinstance(e,(ValueError,RuntimeError,FileNotFoundError)) else 'See local protected diagnostics',file=sys.stderr)
        return 2
    return 0

if __name__=='__main__':raise SystemExit(cli())
