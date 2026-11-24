"""Plan by default. Build trusted sources in fresh staging; never installs an agent."""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]


def iscc_path():
    candidates=[os.environ.get('VYNTRA_ISCC'), shutil.which('ISCC')]
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(str(Path(os.environ['LOCALAPPDATA'])/'Programs/Inno Setup 6/ISCC.exe'))
    for key in ('ProgramFiles(x86)','ProgramFiles'):
        if os.environ.get(key): candidates.append(str(Path(os.environ[key])/'Inno Setup 6/ISCC.exe'))
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()),None)


def stage_sources(root,stage):
    paths=sorted((root/'agent').rglob('*.py'))
    paths += [root/'PrefeituraRemoteCliente.spec',root/'installer/cliente/Vyntra.ico',
              root/'installer/cliente/PrefeituraRemoteCliente.iss',root/'installer/cliente/vyntra-version.txt',
              root/'installer/cliente/vyntra-wizard.bmp',root/'installer/cliente/vyntra-wizard-small.bmp']
    hashes={}
    for source in paths:
        if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError('Source escapes project root')
        relative=source.relative_to(root)
        target=stage/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
        hashes[relative.as_posix()]=hashlib.sha256(target.read_bytes()).hexdigest()
    return hashes


def plan():
    return {'platform':sys.platform,'python':sys.version.split()[0],
            'pyinstaller_available':importlib.util.find_spec('PyInstaller') is not None,
            'inno_available':iscc_path() is not None,
            'installs_agent':False,'desktop_tested':False,'credentials_required_at_build':False}


def inspect_client_manifest(path):
    import pefile
    pe=pefile.PE(str(path))
    try:
        if pe.FILE_HEADER.Machine!=0x8664: raise RuntimeError('Expected x64 client')
        if pe.OPTIONAL_HEADER.Subsystem!=2: raise RuntimeError('Expected Windows GUI subsystem')
        levels=[]
        for resource in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if resource.id != 24: continue
            for item in resource.directory.entries:
                for language in item.directory.entries:
                    data=language.data.struct
                    tree=ET.fromstring(pe.get_data(data.OffsetToData,data.Size))
                    levels.extend(node.attrib for node in tree.iter() if node.tag.endswith('requestedExecutionLevel'))
        if not levels or any(v.get('level')!='asInvoker' or v.get('uiAccess','false')!='false' for v in levels):
            raise RuntimeError('Unexpected elevation/UIAccess manifest')
        return {'architecture':'x64','subsystem':'Windows GUI','execution_level':'asInvoker','ui_access':False}
    finally:
        pe.close()


def execute(output,*,client_only=False,epoch=1794787200):
    if sys.platform!='win32': raise RuntimeError('Windows build requires Windows')
    compiler=iscc_path()
    if not client_only and compiler is None:
        raise RuntimeError('ISCC unavailable; use --client-only for an explicitly partial build')
    if not plan()['pyinstaller_available']: raise RuntimeError('PyInstaller unavailable; no package installed automatically')
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    report_path=output/'build-report.json'
    with report_path.open('x',encoding='utf-8') as report_file:
        report=dict(plan(),status='failed',installer_built=False,client_built=False,
                    signature_verified=False,source_date_epoch=epoch,
                    build_isolation='fresh temporary staging on development host',disposable_guest_used=False)
        try:
            with tempfile.TemporaryDirectory(prefix='prefeitura-build-') as directory:
                stage=Path(directory)
                if not stage.name.startswith('prefeitura-build-') or stage.parent.resolve()!=Path(tempfile.gettempdir()).resolve():
                    raise RuntimeError('Unexpected build staging root')
                report['source_sha256']=stage_sources(ROOT,stage)
                # Names/versions only. Never include raw environment, URLs or credentials.
                packages=sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions() if d.metadata['Name'])
                report['observed_packages']=dict(packages)
                (output/'observed.constraints.txt').write_text(''.join(f'{name}=={version}\n' for name,version in packages),encoding='utf-8')
                env={k:v for k,v in os.environ.items() if not k.upper().startswith(('PREFEITURA_','PYTHONPATH','PYTHONHOME'))}
                env.update(PYTHONHASHSEED='0',SOURCE_DATE_EPOCH=str(epoch),PYTHONDONTWRITEBYTECODE='1',
                           PYINSTALLER_CONFIG_DIR=str(stage/'cache'),TEMP=str(stage/'tmp'),TMP=str(stage/'tmp'),
                           PYTHONIOENCODING='utf-8')
                (stage/'tmp').mkdir()
                command=[sys.executable,'-B','-m','PyInstaller','--noconfirm','--distpath',str(stage/'dist'),
                         '--workpath',str(stage/'work'),'PrefeituraRemoteCliente.spec']
                with (output/'pyinstaller.log').open('xb') as log:
                    built=subprocess.run(command,cwd=stage,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=600,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                if built.returncode: raise RuntimeError('PyInstaller failed; see build log')
                client=stage/'dist/Vyntra.exe'
                report['client_manifest']=inspect_client_manifest(client)
                smoke_path=stage/'self-check.json'
                smoke=subprocess.run([str(client),'--distribution-self-check',str(smoke_path)],cwd=stage,env=env,capture_output=True,
                                     timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
                (output/'self-check.stderr.log').write_bytes(smoke.stderr)
                if smoke.returncode: raise RuntimeError('Packaged client self-check failed')
                (output/'self-check.json').write_bytes(smoke_path.read_bytes())
                result=json.loads(smoke_path.read_text(encoding='utf-8'))
                if (result.get('managed_schema')!='prefeitura-managed-v1'
                        or result.get('acl_exercised') is not False
                        or result.get('tray_module')!='WindowsTray'
                        or result.get('icon_packaged') is not True):
                    raise RuntimeError('Unexpected packaged self-check')
                report['client_self_check']=result
                destination=output/client.name
                with destination.open('xb') as file: file.write(client.read_bytes())
                report.update(client_built=True,client_sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
                if not client_only:
                    with (output/'inno.log').open('xb') as log:
                        installed=subprocess.run([compiler,'installer/cliente/PrefeituraRemoteCliente.iss'],cwd=stage,env=env,
                                                 stdout=log,stderr=subprocess.STDOUT,timeout=300,creationflags=subprocess.CREATE_NO_WINDOW)
                    if installed.returncode: raise RuntimeError('ISCC failed; see build log')
                    setup=stage/'dist/Vyntra-Setup.exe'
                    with (output/setup.name).open('xb') as file: file.write(setup.read_bytes())
                    report.update(installer_built=True,installer_sha256=hashlib.sha256(setup.read_bytes()).hexdigest())
                report['status']='client_only' if client_only else 'packages_built_not_installed'
        except Exception as error:
            report['error_type']=type(error).__name__
            report['status']='failed'
        json.dump(report,report_file,indent=2)
    print(json.dumps({k:report[k] for k in ('status','client_built','installer_built','signature_verified')},indent=2))
    return int(report['status']=='failed')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--client-only',action='store_true')
    parser.add_argument('--output',default='artifacts/windows-build')
    parser.add_argument('--source-date-epoch',type=int,default=1794787200)
    args=parser.parse_args()
    if not args.execute:
        print(json.dumps(dict(plan(),status='plan_only'),indent=2)); return 0
    if args.source_date_epoch<315532800: parser.error('SOURCE_DATE_EPOCH must be at least 1980-01-01')
    return execute(args.output,client_only=args.client_only,epoch=args.source_date_epoch)


if __name__=='__main__': raise SystemExit(main())
