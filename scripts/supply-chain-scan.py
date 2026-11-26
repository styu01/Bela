#!/usr/bin/env python3
"""Bounded, read-only filesystem inventory against local threat catalogs."""
import sys
sys.dont_write_bytecode=True
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import time
try:
    import tomllib
except ImportError:
    tomllib=None
import unicodedata
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
_spec=importlib.util.spec_from_file_location('supply_kanban_common',ROOT/'scripts/kanban-audit.py')
common=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(common)
Error=common.AuditError
DEFAULT_ROOTS=('/home/kisss',)
EDITORS=('.vscode','.vscode-server','.cursor','.windsurf','.vscode-insiders','.antigravity','.kiro','.trae','.vscodium')
WINDOWS_AUTO_EDITORS=('.vscode','.cursor','.vscode-insiders')
SYSTEM_ROOTS=('/usr/lib/python3/dist-packages','/usr/lib/node_modules','/usr/local/lib/node_modules')
PYTHON_LOCAL_LIB=Path('/usr/local/lib')
WINDOWS_USERS=Path('/mnt/c/Users')
CATALOG=Path('/home/kisss/.claude/tools/bumblebee-threat-intel')
STATE='supply-chain-scan-state.json'
ACK='supply-chain-ack.json'
PACKAGE_TREES=('node_modules','site-packages','dist-packages')
ECOS=('npm','pypi','rubygems','go','editor-extension','packagist')
MAX_PACKAGE_DEPTH=64
MAX_DEPTH=24
MAX_FILES=600000
MAX_SECONDS=60
MAX_BYTES=8*1024*1024
DAY=86400
SCHEMAS=('0.1.0','0.2.0')
BUDAPEST=ZoneInfo('Europe/Budapest')
CODES={'catalog_missing','catalog_unusable','catalog_schema_unknown','state_read_failed','state_write_failed',
       'ack_invalid','ack_read_failed','ack_path_invalid','ack_no_issue','ack_path_changed','lock_failed','invalid_now','invalid_limits','clock_rollback','scan_failed','dashboard_credentials_unavailable',
       'dashboard_http_error','dashboard_unavailable','dashboard_invalid_response','invalid_dashboard_url'}


def children(path,budget=None,errors=None):
    try:fd,base=open_dir(path)
    except FileNotFoundError:return []
    except OSError:
        if errors is not None:errors.append(path)
        return []
    try:
        result=[]
        with os.scandir(fd) as entries:
            for entry in entries:
                if budget is not None:budget.check()
                if not entry.is_symlink() and entry.is_dir(follow_symlinks=False):result.append(base/entry.name)
        return result
    except OSError:
        if errors is not None:errors.append(path)
        return []
    finally:os.close(fd)


def optional_directory(path):
    try:return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:return False
    except OSError:return True  # A known inaccessible root must become incomplete during traversal.


def windows_extension_root(path):
    p=Path(path).parts
    return (len(p)==7 and p[:2]==('/','mnt') and re.fullmatch('[A-Za-z]',p[2]) is not None
            and p[3]=='Users' and p[4] not in ('.','..') and p[5] in EDITORS and p[6]=='extensions')


def default_roots(budget=None,errors=None):
    result=[Path(p) for p in DEFAULT_ROOTS]
    result.extend(Path(p) for p in SYSTEM_ROOTS if optional_directory(p))
    for library in children(PYTHON_LOCAL_LIB,budget,errors):
        target=library/'dist-packages'
        if library.name.startswith('python3') and optional_directory(target):result.append(target)
    for user in children(WINDOWS_USERS,budget,errors):
        for editor in WINDOWS_AUTO_EDITORS:
            target=user/editor/'extensions'
            if optional_directory(target):result.append(target)
    return result


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True).encode()).hexdigest()


def clean(value,limit=200):
    value=str(value) if not isinstance(value,str) else value
    value=''.join(' ' if unicodedata.category(c).startswith('C') or c in '\u2013\u2014' else c for c in value)
    return ' '.join(value.replace('--',' ').split())[:limit].encode('ascii','replace').decode()


def name(eco,value):
    if not isinstance(value,str) or not value.strip() or len(value)>512:raise ValueError()
    value=value.strip()
    if eco=='pypi':return re.sub(r'[-_.]+','-',value.lower())
    return value if eco=='go' else value.lower()


PEP=re.compile(r'v?(?:(\d+)!)?(\d+(?:\.\d+)*)(?:[-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?(\d*))?(?:(?:[-_.]?(post|rev|r)[-_.]?(\d*))|-(\d+))?(?:[-_.]?(dev)[-_.]?(\d*))?(?:\+([a-z0-9]+(?:[-_.][a-z0-9]+)*))?',re.I)
def version(eco,value):
    if not isinstance(value,str) or not value.strip() or len(value)>256:raise ValueError()
    value=value.strip()
    if eco=='rubygems':
        return re.sub(r'-(?:(?:[A-Za-z0-9_]+-)*(?:linux|darwin|mingw[A-Za-z0-9_]*|java)(?:[-_][A-Za-z0-9_.-]+)?|x86(?:_64)?|x64|i[3-6]86|aarch64|arm(?:64|v[0-9]+)?|riscv64|ppc64le|s390x|universal|jruby|ruby|mswin(?:32|64)?|mingw(?:32|64)?)(?:[-_].*)?$','',value)
    if eco=='packagist':return re.sub(r'^[vV]','',value)
    if eco=='npm':return re.sub(r'^[vV]','',value).split('+',1)[0]
    if eco=='pypi':
        match=PEP.fullmatch(value.lower())
        if not match:raise ValueError()
        epoch,release,pre,pn,post,postn,postdash,dev,devn,local=match.groups()
        nums=[int(n) for n in release.split('.')]
        while len(nums)>1 and nums[-1]==0:nums.pop()
        pre={'alpha':'a','beta':'b','c':'rc','pre':'rc','preview':'rc'}.get(pre,pre)
        return (str(int(epoch or 0))+'!'+'.'.join(map(str,nums))
            +(pre+str(int(pn or 0)) if pre else '')
            +('.post'+str(int(postn or postdash or 0)) if post or postdash is not None else '')
            +('.dev'+str(int(devn or 0)) if dev else '')
            +('+'+'.'.join(str(int(n)) if n.isdigit() else n for n in re.split(r'[-_.]',local)) if local else ''))
    return value  # Go deliberately keeps v and +incompatible.


def open_dir(path):
    """Open every ancestor with NOFOLLOW, including explicitly supplied roots."""
    path=Path(os.path.abspath(os.path.expanduser(str(path))))
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd,path
    except BaseException:os.close(fd);raise


def read_bytes_at(fd,entry):
    child=os.open(entry.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
        info=os.fstat(child);before=entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(before.st_dev,before.st_ino):raise OSError('changed')
        if info.st_size>MAX_BYTES:raise ValueError('large')
        with os.fdopen(child,'rb',closefd=False) as stream:raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('large')
        after=os.fstat(child)
        if (after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(info.st_size,info.st_mtime_ns,info.st_ctime_ns):raise OSError('changed')
        return raw,info
    finally:os.close(child)


def read_at(fd,entry):
    raw,info=read_bytes_at(fd,entry)
    return raw.decode('utf-8-sig'),info


class ControlManifest(Exception):pass


def parse_kind(exc):
    return ('manifest_json' if isinstance(exc,json.JSONDecodeError) else
            'manifest_toml' if tomllib is not None and isinstance(exc,tomllib.TOMLDecodeError) else
            'manifest_encoding' if isinstance(exc,UnicodeError) else
            'manifest_missing_field' if isinstance(exc,KeyError) else
            'manifest_type' if isinstance(exc,TypeError) else 'manifest_parse_or_limit')


def named_bytes(fd,filename):
    class Entry:
        name=filename
        def stat(self,**kwargs):return os.stat(filename,dir_fd=fd,follow_symlinks=False)
    return read_bytes_at(fd,Entry())


def valid_package(fd):
    try:
        raw,_=named_bytes(fd,'package.json');data=json.loads(raw.decode('utf-8-sig'))
        name('npm',data['name']);version('npm',data['version'])
        return True
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError):return False


def read_ack(path):
    try:fd,_=open_dir(path.parent)
    except FileNotFoundError:return []
    except OSError:raise Error('ack_read_failed') from None
    try:
        try:raw,_=named_bytes(fd,path.name)
        except FileNotFoundError:return []
        data=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data,dict) or set(data)!={'acknowledged'} or not isinstance(data['acknowledged'],list) or len(data['acknowledged'])>10000:raise ValueError()
        result=[];seen=set()
        for item in data['acknowledged']:
            if not isinstance(item,dict):raise ValueError()
            required=('path','kind','size','sha256','note','acked_at')
            if set(item)!=set(required):raise ValueError()
            if not all(isinstance(item[k],str) for k in ('path','kind','sha256','note','acked_at')):raise ValueError()
            if not os.path.isabs(item['path']) or os.path.abspath(item['path'])!=item['path']:raise ValueError()
            if item['kind'] not in ('manifest_json','manifest_toml','manifest_encoding','manifest_missing_field','manifest_type','manifest_parse_or_limit','invalid_package_record'):raise ValueError()
            if not re.fullmatch('[0-9a-f]{64}',item['sha256']) or type(item['size']) is not int or not 0<=item['size']<=MAX_BYTES:raise ValueError()
            if len(item['note'])>1000:raise ValueError()
            stamp=datetime.fromisoformat(item['acked_at'].replace('Z','+00:00'))
            if stamp.tzinfo is None:raise ValueError()
            key=(item['path'],item['kind'],item['sha256'])
            if key in seen:raise ValueError()
            seen.add(key);result.append({k:item[k] for k in required})
        return result
    except OSError:raise Error('ack_read_failed') from None
    except (ValueError,TypeError,KeyError,UnicodeError,RecursionError):raise Error('ack_invalid') from None
    finally:os.close(fd)


def parent_package(path):
    # Only ancestors within the nearest node_modules package may grant control status.
    for folder in path.parent.parents:
        if folder.name=='node_modules':break
        if 'node_modules' not in folder.parts:break
        try:fd,_=open_dir(folder)
        except OSError:return False
        try:
            if valid_package(fd):return True
        finally:os.close(fd)
    return False


FIXTURE_PARTS=frozenset(('test','tests','__tests__','fixtures','__fixtures__','mocks'))

def fixture_manifest(path):
    if path.name!='package.json' or 'node_modules' not in path.parts:return False
    parts=path.parts;start=max(i for i,p in enumerate(parts) if p=='node_modules')+1
    if start>=len(parts)-1:return False
    # Only this installed package's own subdirectories; never the project prefix.
    start+=2 if parts[start].startswith('@') else 1
    return any(part in FIXTURE_PARTS for part in parts[start:-1])


def inspect_issue(path):
    path=Path(os.path.abspath(os.path.expanduser(str(path))))
    if not relevant(path.name,path.parts):raise Error('ack_path_invalid')
    try:
        fd,_=open_dir(path.parent)
        try:raw,info=named_bytes(fd,path.name)
        finally:os.close(fd)
    except (OSError,ValueError):raise Error('ack_path_invalid') from None
    try:
        for eco,package,ver in records(path.name,raw.decode('utf-8-sig'),path.parts,parent_package(path)):
            try:name(eco,package);version(eco,ver)
            except (ValueError,TypeError,OverflowError):return path,'invalid_package_record',raw,info
    except ControlManifest:raise Error('ack_no_issue') from None
    except (ValueError,TypeError,KeyError,IndexError,UnicodeError,RecursionError) as exc:
        if fixture_manifest(path):raise Error('ack_no_issue') from None
        return path,parse_kind(exc),raw,info
    raise Error('ack_no_issue')


class Budget:
    def __init__(self,seconds):self.start=time.monotonic();self.seconds=seconds
    def check(self):
        if time.monotonic()-self.start>=self.seconds:raise TimeoutError()


class Catalog:
    def __init__(self):self.index={};self.entries=0;self.errors=0;self.newest=None;self.unknown_schema=0;self.unsupported=0
    def load(self,path,budget):
        try:fd,_=open_dir(path)
        except OSError:raise Error('catalog_missing') from None
        files=0
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    budget.check()
                    if not entry.name.endswith('.json') or entry.is_symlink():continue
                    files+=1
                    try:
                        raw,info=read_at(fd,entry);data=json.loads(raw)
                        if not isinstance(data,dict):raise ValueError()
                        if data.get('schema_version') not in SCHEMAS:
                            self.errors+=1;self.unknown_schema+=1;continue
                        items=data.get('entries')
                        if not isinstance(items,list):raise ValueError()
                        usable=False
                        for item in items:
                            budget.check()
                            try:
                                if not isinstance(item,dict):raise ValueError()
                                eco=item.get('ecosystem')
                                if not isinstance(eco,str) or not eco:raise ValueError()
                                if eco not in ECOS:self.unsupported+=1;continue
                                eco=item['ecosystem'];package=name(eco,item.get('package'));ident=item.get('id')
                                if not isinstance(ident,str) or not ident or len(ident)>256:raise ValueError()
                                if any(not isinstance(item.get(k),str) or not item[k] for k in ('name','severity','source')):raise ValueError()
                                versions=item.get('versions')
                                if not isinstance(versions,list) or not versions:raise ValueError()
                                normalized={version(eco,v) for v in versions}
                                for ver in normalized:self.index.setdefault((eco,package),{}).setdefault(ver,set()).add(ident)
                                self.entries+=1;usable=True
                            except (ValueError,TypeError,KeyError,OverflowError):self.errors+=1
                        if usable:self.newest=max(self.newest or 0,info.st_mtime)
                    except (OSError,ValueError,UnicodeError,RecursionError,TypeError):self.errors+=1
        finally:os.close(fd)
        if not files:raise Error('catalog_missing')
        if not self.entries:raise Error('catalog_unusable')
        return self


def npm_selector(selector):
    match=re.fullmatch(r'(@[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+|[A-Za-z0-9._~-]+)@(.+)',selector)
    if not match:return None
    package,ver=match.groups()
    if ver.startswith('npm:'):
        target=ver[4:]
        # A registry protocol and an alias both use npm:, but only aliases include a target name.
        parsed=npm_selector(target) if '@' in target else None
        return parsed or (package,target)
    return package,ver


def npm_record(package,ver):
    if isinstance(ver,str) and ver.startswith('npm:'):
        parsed=npm_selector(ver[4:])
        if parsed:return 'npm',*parsed
    return 'npm',package,ver


def launcher_name(token):
    return re.split(r'[\\/]',token)[-1].lower().removesuffix('.cmd').removesuffix('.exe')


def launcher_tokens(command,args):
    # Static tokenization only; no shell or environment evaluation, max eight wrappers.
    tokens=([command.strip('"')] if re.match(r'^"?[A-Za-z]:[\\/]',command) else shlex.split(command))+args
    for _ in range(8):
        if not tokens:return []
        tool=launcher_name(tokens[0])
        if tool=='env':
            i=1
            while i<len(tokens) and (tokens[i] in ('--','-i') or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=',tokens[i])):i+=1
            tokens=tokens[i:];continue
        if tool in ('cmd','bash','sh') and len(tokens)>1 and tokens[1].lower() in ('/c','-c','-lc'):
            rest=tokens[2:]
            if not rest:return []
            if re.match(r'^"?[A-Za-z]:[\\/]',rest[0]):tokens=[rest[0].strip('"')]+rest[1:]
            else:tokens=shlex.split(rest[0])+rest[1:] if len(rest)==1 or ' ' in rest[0] else rest
            continue
        return tokens
    return []


def unsupported_lock_type(filename):
    known=('.package-lock.json','Cargo.lock','bun.lockb','deno.lock','flake.lock','go.work','go.work.sum','mix.lock','gradle.lockfile','packages.lock.json')
    if filename in known:return filename
    for suffix in ('.lockb','-lock.json','.lock.json','.lockfile','.lock'):
        if filename.endswith(suffix):return '*'+suffix
    return None


def bounded_lock_types(counts):
    ordered=sorted(counts.items(),key=lambda v:(-v[1],v[0]))
    if len(ordered)<=10:return dict(ordered)
    return dict(ordered[:9],other=sum(v for k,v in ordered[9:]))


def mcp_records(data,counters=None):
    stack=[data]
    while stack:
        item=stack.pop()
        if isinstance(item,dict):
            command=item.get('command');args=item.get('args',[])
            if isinstance(command,str):
                bad=sum(not isinstance(v,str) for v in args) if isinstance(args,list) else 1
                if counters is not None:counters['mcp_non_string_args']+=bad
                strings=[v for v in args if isinstance(v,str)] if isinstance(args,list) else []
                try:tokens=launcher_tokens(command,strings)
                except ValueError:tokens=[]
                if tokens:
                    tool=launcher_name(tokens[0])
                    eco='pypi' if tool=='uvx' else 'npm';start=1
                    supported=tool in ('npx','pnpx','uvx','bunx')
                    if len(tokens)>1 and (tool,tokens[1]) in (('npm','exec'),('pnpm','exec'),('pnpm','dlx'),('yarn','dlx'),('bun','x')):
                        supported=True;start=2
                    if supported:
                        for token in tokens[start:]:
                            token=token.removeprefix('--package=').removeprefix('--from=')
                            if token.startswith('-'):continue
                            if tool=='uvx' and '==' in token:
                                package,ver=token.split('==',1);yield eco,package,ver
                            else:
                                parsed=npm_selector(token)
                                if parsed:yield eco,*parsed
            for key,value in item.items():
                if key not in ('env','args','command') and isinstance(value,(dict,list)):stack.append(value)
        elif isinstance(item,list):stack.extend(v for v in item if isinstance(v,(dict,list)))


def jsonc(text):
    # Strip JSONC comments/trailing commas only outside strings; never evaluate code.
    out=[];i=0;quoted=False;escaped=False
    while i<len(text):
        c=text[i]
        if quoted:
            out.append(c)
            if escaped:escaped=False
            elif c=='\\':escaped=True
            elif c=='"':quoted=False
            i+=1;continue
        if c=='"':quoted=True;out.append(c);i+=1;continue
        if text.startswith('//',i):
            end=text.find('\n',i+2);i=len(text) if end<0 else end;continue
        if text.startswith('/*',i):
            end=text.find('*/',i+2)
            if end<0:raise ValueError()
            out.append(' ');i=end+2;continue
        out.append(c);i+=1
    text=''.join(out);out=[];quoted=False;escaped=False
    for i,c in enumerate(text):
        if quoted:
            out.append(c)
            if escaped:escaped=False
            elif c=='\\':escaped=True
            elif c=='"':quoted=False
        elif c=='"':quoted=True;out.append(c)
        elif c==',':
            j=i+1
            while j<len(text) and text[j].isspace():j+=1
            if j>=len(text) or text[j] not in '}]':out.append(c)
        else:out.append(c)
    return json.loads(''.join(out))


def requirements_file(filename,parts):
    return (re.fullmatch(r'(?:(?:requirements|constraints).*|.*-requirements)\.txt',filename) is not None or
            filename.endswith('.txt') and any(p in ('requirements','constraints') for p in parts[:-1]))


def records(filename,text,parts,parent_valid=False,counters=None):
    if filename=='package.json':
        data=json.loads(text)
        if not isinstance(data,dict):raise ValueError()
        relative=parts[max(i for i,p in enumerate(parts) if p=='node_modules')+1:-1]
        package_root=(len(relative)==1 and not relative[0].startswith('@')) or (len(relative)==2 and relative[0].startswith('@'))
        if not package_root and ('name' not in data or 'version' not in data) and parent_valid:raise ControlManifest()
        yield 'npm',data['name'],data['version']
    elif filename in ('composer.lock','installed.json'):
        data=json.loads(text)
        if filename=='composer.lock':
            if not isinstance(data,dict) or not any(k in data for k in ('packages','packages-dev')):raise ValueError()
            groups=[data.get('packages',[]),data.get('packages-dev',[])]
        else:
            groups=[data.get('packages') if isinstance(data,dict) else data]
        for items in groups:
            if not isinstance(items,list):raise ValueError()
            for item in items:
                if not isinstance(item,dict):raise ValueError()
                yield 'packagist',item['name'],item['version']
    elif filename in ('package-lock.json','npm-shrinkwrap.json'):
        data=json.loads(text)
        if not isinstance(data,dict):raise ValueError()
        if data.get('lockfileVersion',1) not in (1,2,3):raise ValueError()
        if 'packages' not in data and 'dependencies' not in data:raise ValueError()
        packages=data.get('packages')
        if isinstance(packages,dict):
            for key,item in packages.items():
                if not isinstance(item,dict):raise ValueError()
                if 'node_modules/' in key and isinstance(item.get('version'),str):
                    yield npm_record(item.get('name') or key.rsplit('node_modules/',1)[1],item['version'])
        dependencies=data.get('dependencies',{})
        if not isinstance(dependencies,dict):raise ValueError()
        stack=[dependencies]
        while stack:
            for package,item in stack.pop().items():
                if not isinstance(item,dict):raise ValueError()
                if isinstance(item.get('version'),str):yield npm_record(package,item['version'])
                nested=item.get('dependencies',{})
                if not isinstance(nested,dict):raise ValueError()
                if nested:stack.append(nested)
    elif filename=='yarn.lock':
        names=[];ver=None;resolution=None;saw_header=False
        def emit():
            if names and ver is None and resolution is None:raise ValueError()
            if resolution:
                yield 'npm',resolution[0],resolution[1]
            elif ver is not None:
                for package in names:yield 'npm',package,ver
        for line in text.splitlines():
            if line and not line[0].isspace() and line.rstrip().endswith(':'):
                yield from emit();names=[];ver=None;resolution=None
                if line.strip()=='__metadata:':saw_header=True
                for token in line.rstrip()[:-1].split(','):
                    parsed=npm_selector(token.strip().strip('"\''))
                    if parsed:names.append(parsed[0]);saw_header=True
            match=re.match(r'^ {2}version(?:\s+|:\s*)["\']?([^\s"\']+)',line)
            if match:ver=match[1]
            match=re.match(r'^ {2}resolution:\s*["\']?([^\s"\']+)',line)
            if match:
                parsed=npm_selector(match[1])
                if parsed:resolution=parsed
        yield from emit()
        if text.strip() and not saw_header and any(x.strip() and not x.lstrip().startswith('#') for x in text.splitlines()):raise ValueError()
    elif filename=='bun.lock':
        data=jsonc(text)
        if not isinstance(data,dict) or not isinstance(data.get('packages'),dict):raise ValueError()
        for item in data['packages'].values():
            if not isinstance(item,list) or not item or not isinstance(item[0],str):raise ValueError()
            parsed=npm_selector(item[0])
            if not parsed:raise ValueError()
            yield 'npm',*parsed
    elif filename=='Pipfile.lock':
        data=json.loads(text)
        if not isinstance(data,dict) or not any(k in data for k in ('default','develop')):raise ValueError()
        for group in ('default','develop'):
            items=data.get(group,{})
            if not isinstance(items,dict):raise ValueError()
            for package,item in items.items():
                if not isinstance(item,dict):raise ValueError()
                ver=item.get('version')
                if isinstance(ver,str):
                    match=re.fullmatch(r'(?:===|==)([^\s]+)',ver)
                    if match:yield 'pypi',package,match[1]
    elif filename=='pnpm-lock.yaml':
        if text.strip() and not re.search(r'(?m)^(?:lockfileVersion|packages|snapshots|importers):',text):raise ValueError()
        section=''
        for line in text.splitlines():
            if line and not line[0].isspace():section=line.strip()
            if section not in ('packages:','snapshots:'):continue
            match=re.match(r'^ {2}["\']?(.+?)["\']?:\s*$',line)
            if not match:continue
            key=match[1].strip('"\'').split('(',1)[0].lstrip('/')
            parsed=npm_selector(key)
            if parsed:yield 'npm',parsed[0],parsed[1].split('_',1)[0]
            elif '/' in key:
                package,ver=key.rsplit('/',1)
                if re.match(r'^\d',ver):yield 'npm',package,ver.split('_',1)[0]
    elif filename in ('METADATA','PKG-INFO') or filename.endswith('.egg-info'):
        data={}
        for line in text.splitlines():
            if not line.strip():break
            if line.startswith(('Name:','Version:')):
                key,value=line.split(':',1)
                if key in data:raise ValueError()
                data[key]=value.strip()
        yield 'pypi',data['Name'],data['Version']
    elif requirements_file(filename,parts):
        for line in text.splitlines():
            match=re.match(r'^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?\s*(?:===|==)\s*([^\s;#]+)',line)
            if match:yield 'pypi',match[1],match[2]
    elif filename in ('poetry.lock','uv.lock','pdm.lock'):
        if tomllib is None:
            if counters is not None:counters['unsupported_config']+=1
            return
        data=tomllib.loads(text)
        items=data.get('package')
        if not isinstance(items,list):raise ValueError()
        for item in items:
            if not isinstance(item,dict):raise ValueError()
            if 'version' not in item or item['version'] is None:
                if counters is not None:counters['lock_entries_without_version']+=1
                continue
            yield 'pypi',item['name'],item['version']
    elif filename=='Gemfile.lock':
        if text.strip() and not re.search(r'(?m)^(?:GEM|GIT|PATH|DEPENDENCIES|PLATFORMS|BUNDLED WITH)$',text):raise ValueError()
        for line in text.splitlines():
            match=re.match(r'^ {4}([^\s]+) \(([^)]+)\)',line)
            if match:yield 'rubygems',match[1],match[2]
    elif filename in ('go.mod','go.sum'):
        if filename=='go.mod' and text.strip() and not re.search(r'(?m)^module\s+\S+',text):raise ValueError()
        block=False
        for line in text.splitlines():
            line=line.split('//',1)[0].strip()
            if line.startswith('require ('):block=True;continue
            if line==')':block=False;continue
            if filename=='go.sum' or block:tokens=line.split()
            elif line.startswith('require '):tokens=line.split()[1:]
            else:continue
            if len(tokens)>=2 and tokens[1].startswith('v'):yield 'go',tokens[0],tokens[1].removesuffix('/go.mod')
            elif filename=='go.sum' and line:raise ValueError()
    elif filename=='config.toml':
        if tomllib is None:
            if counters is not None:counters['unsupported_config']+=1
            return
        yield from mcp_records(tomllib.loads(text),counters)
    else:yield from mcp_records(json.loads(text),counters)


def relevant(filename,parts):
    return (filename in ('claude_desktop_config.json','bun.lock','Pipfile.lock','pdm.lock','npm-shrinkwrap.json','composer.lock','package-lock.json','yarn.lock','pnpm-lock.yaml','poetry.lock','uv.lock','Gemfile.lock','go.mod','go.sum','.mcp.json','.claude.json')
        or filename=='installed.json' and len(parts)>2 and tuple(parts[-3:-1])==('vendor','composer')
        or filename=='package.json' and 'node_modules' in parts
        or filename.endswith('.egg-info')
        or filename=='METADATA' and any(p.endswith('.dist-info') for p in parts)
        or filename=='PKG-INFO' and any(p.endswith('.egg-info') for p in parts)
        or requirements_file(filename,parts)
        or filename in ('settings.json','settings.local.json') and len(parts)>1 and parts[-2]=='.claude'
        or filename=='mcp.json' and len(parts)>1 and parts[-2] in ('.cursor','.vscode')
        or filename=='mcp_config.json' and len(parts)>2 and tuple(parts[-3:-1])==('.codeium','windsurf')
        or filename=='settings.json' and len(parts)>1 and parts[-2]=='.gemini'
        or filename=='config.toml' and len(parts)>1 and parts[-2]=='.codex'
        or filename=='settings.json' and len(parts)>3 and parts[-2]=='.claude-config' and parts[-4]=='agents')


class Inventory:
    def __init__(self,catalog,budget,max_depth,max_files):
        self.catalog=catalog;self.budget=budget;self.max_depth=max_depth;self.max_files=max_files
        self.counts={eco:0 for eco in ECOS};self.files=0;self.other=0;self.errors=0;self.links=0
        self.findings={};self.reasons=set();self.ignored_io_errors=0
        self.parser_counts={"mcp_non_string_args":0,"unsupported_config":0,"lock_entries_without_version":0};self.unsupported_lockfiles=0;self.unsupported_types={}
        self.depth_limit_dirs=[];self.error_samples=[]
        self.acks={};self.acknowledged=[];self.ack_seen=set();self.ignored_control_manifests=0;self.ignored_fixture_manifests=0

    def add(self,eco,package,ver,path,issue_bytes=None):
        self.budget.check()
        try:package=name(eco,package);ver=version(eco,ver)
        except (ValueError,TypeError,OverflowError):self.record_error("invalid_package_record",path,raw=issue_bytes);return
        self.counts[eco]+=1
        known=self.catalog.index.get((eco,package))
        if known is None:return
        ids=known.get(ver)
        if not ids:self.other+=1;return
        key=digest([eco,package,ver,sorted(ids)])
        finding=self.findings.setdefault(key,{'ecosystem':eco,'package':package,'version':ver,'catalog_ids':sorted(ids),'locations':[]})
        if str(path) not in finding['locations'] and len(finding['locations'])<5:finding['locations'].append(str(path))

    def directory_record(self,path):
        if path.parent.name=='gems':
            match=re.fullmatch(r'(.+)-(\d[^/]*)',version('rubygems',path.name))
            if match:self.add('rubygems',match[1],match[2],path)
        if path.parent.name=='extensions' and path.parent.parent.name in EDITORS:
            match=re.fullmatch(r'(.+\..+?)-(\d+(?:\.\d+){1,3}(?:[-+].*)?)',path.name)
            if match:
                ver=re.sub(r'-(?:universal|java|(?:linuxmusl|linux|darwin|win32|alpine|web)(?:-(?:universal|x64|arm64|armhf|ia32))?)$','',match[2])
                self.add('editor-extension',match[1],ver,path)

    def walk(self,path):
        path=Path(os.path.abspath(os.path.expanduser(str(path))))
        if ((str(path)=='/mnt' or str(path).startswith('/mnt/')) and not windows_extension_root(path) or any(part in ('.git','.cache') for part in path.parts)
                or '/.local/share/Trash' in str(path)):
            self.reasons.add('excluded_root');return
        try:fd,path=open_dir(path)
        except OSError:self.reasons.add('root_unreadable');return
        self._walk(fd,path,0,0 if any(p in PACKAGE_TREES for p in path.parts) else None,windows=windows_extension_root(path))

    def record_error(self,kind,path,ignored=False,raw=None):
        if not ignored and raw is not None:
            key=(str(path),kind,hashlib.sha256(raw).hexdigest())
            ack=self.acks.get(key)
            if ack is not None and ack['size']==len(raw):
                if key not in self.ack_seen:
                    self.ack_seen.add(key)
                    if len(self.acknowledged)<20:
                        self.acknowledged.append({'kind':kind,'path':clean(str(path),160),'size':len(raw),'sha256':key[2]})
                return
        if ignored:self.ignored_io_errors+=1
        else:self.errors+=1
        if len(self.error_samples)<15:
            self.error_samples.append({'kind':kind,'path':clean(str(path),160)})

    def _walk(self,fd,path,depth,package_depth=None,parent_valid=False,windows=False):
        try:
            self.directory_record(path)
            # Pre-read independent of scandir ordering; only grant status to descendants.
            if path.name=='node_modules':parent_valid=False
            valid_here=valid_package(fd) if 'node_modules' in path.parts else False
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.budget.check()
                    if 'file_limit' in self.reasons:return
                    child=path/entry.name
                    is_directory=False
                    try:
                        if entry.is_symlink():self.links+=1;continue
                        if (str(child)=='/mnt' or str(child).startswith('/mnt/')) and not windows:continue
                        if entry.name in ('.git','.cache') or str(child).endswith('/.local/share/Trash'):continue
                        if entry.is_dir(follow_symlinks=False):
                            is_directory=True
                            next_package=(package_depth+1 if package_depth is not None else 0 if entry.name in PACKAGE_TREES else None)
                            limited=(next_package>MAX_PACKAGE_DEPTH if next_package is not None else depth>=self.max_depth)
                            if limited:
                                self.reasons.add('depth_limit')
                                if len(self.depth_limit_dirs)<5:self.depth_limit_dirs.append(clean(str(child),160))
                                continue
                            sub=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                            self._walk(sub,child,depth+1,next_package,parent_valid or valid_here,windows)
                        elif entry.is_file(follow_symlinks=False):
                            self.files+=1
                            if self.files>=self.max_files:self.reasons.add('file_limit')
                            if not relevant(entry.name,child.parts):
                                kind=unsupported_lock_type(entry.name)
                                if kind:
                                    self.unsupported_lockfiles+=1;self.unsupported_types[kind]=self.unsupported_types.get(kind,0)+1
                                continue
                            try:
                                raw_bytes=None
                                raw_bytes,_=read_bytes_at(fd,entry)
                                for eco,package,ver in records(entry.name,raw_bytes.decode('utf-8-sig'),child.parts,parent_valid,self.parser_counts):
                                    self.add(eco,package,ver,child,raw_bytes)
                            except ControlManifest:self.ignored_control_manifests+=1
                            except (ValueError,TypeError,KeyError,IndexError,UnicodeError,RecursionError) as exc:
                                if raw_bytes is not None and fixture_manifest(child):self.ignored_fixture_manifests+=1
                                else:self.record_error(parse_kind(exc),child,raw=raw_bytes)
                    except OSError:
                        # A metadata failure may hide a directory, not just an irrelevant file.
                        # Ignore only a positively identified regular, unrelated file.
                        if not is_directory and not relevant(entry.name,child.parts):
                            try:is_directory=not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode)
                            except OSError:is_directory=True
                        self.record_error('directory_io' if is_directory else 'manifest_io' if relevant(entry.name,child.parts) else 'unrelated_io',
                                          child,ignored=not is_directory and not relevant(entry.name,child.parts))
        except OSError:self.record_error('directory_io',path)
        finally:os.close(fd)


def read_state(path,now):
    try:
        parent,_=open_dir(path.parent)
    except FileNotFoundError:return {}
    except OSError:raise Error('state_read_failed') from None
    try:
        try:fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        except FileNotFoundError:return {}
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES:raise OSError()
            with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:raise OSError()
            value=json.loads(raw.decode('utf-8-sig'))
        finally:os.close(fd)
    except (ValueError,UnicodeError,RecursionError):return {}
    except OSError:raise Error('state_read_failed') from None
    finally:os.close(parent)
    if not isinstance(value,dict):return {}
    notified=value.get('notified',{})
    value['notified']={k:v for k,v in notified.items() if isinstance(k,str) and re.fullmatch(r'[0-9a-f]{64}',k)
                       and common.epoch(v) and v<=now} if isinstance(notified,dict) else {}
    streak=value.get('failure_streak',0)
    result={'notified':value['notified'],'failure_streak':min(streak,1000000) if type(streak) is int and streak>=0 else 0}
    if common.epoch(value.get('last_run_at')):result['last_run_at']=value['last_run_at']
    if value.get('last_status') in ('clean','findings','incomplete','error'):result['last_status']=value['last_status']
    result['failure_notified']=value.get('failure_notified') is True and result.get('last_status') in ('incomplete','error')
    if isinstance(value.get('last_findings_hash'),str) and re.fullmatch(r'[0-9a-f]{64}',value['last_findings_hash']):
        result['last_findings_hash']=value['last_findings_hash']
    if common.epoch(value.get('catalog_newest_mtime')):result['catalog_newest_mtime']=value['catalog_newest_mtime']
    if isinstance(value.get('catalog_notice_month'),str) and re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])',value['catalog_notice_month']):
        result['catalog_notice_month']=value['catalog_notice_month']
    if isinstance(value.get('incomplete_reasons_hash'),str) and re.fullmatch(r'[0-9a-f]{64}',value['incomplete_reasons_hash']):
        result['incomplete_reasons_hash']=value['incomplete_reasons_hash']
    if common.epoch(value.get('incomplete_notified_at')) and value['incomplete_notified_at']<=now:
        result['incomplete_notified_at']=value['incomplete_notified_at']
    pending=value.get('pending_notification')
    if pending is not None:
        if not isinstance(pending,dict):raise Error('state_read_failed')
        content=pending.get('content');keys=pending.get('finding_keys')
        if (not isinstance(content,str) or not content.startswith('[SUPPLY-CHAIN] ') or len(content)>40000 or
                any(ord(c)<32 and c!='\n' or ord(c)>126 for c in content) or not isinstance(keys,list) or len(keys)>20 or
                any(not isinstance(k,str) or not re.fullmatch('[0-9a-f]{64}',k) for k in keys)):
            raise Error('state_read_failed')
        rh=pending.get('incomplete_reasons_hash');month=pending.get('catalog_notice_month')
        if rh is not None and (not isinstance(rh,str) or not re.fullmatch('[0-9a-f]{64}',rh)):raise Error('state_read_failed')
        if month is not None and (not isinstance(month,str) or not re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])',month)):raise Error('state_read_failed')
        result['pending_notification']={'content':content,'finding_keys':keys,'incomplete_reasons_hash':rh,'catalog_notice_month':month}
    return result


def public_finding(finding):
    return {'ecosystem':clean(finding['ecosystem'],40),'package':clean(finding['package'],512),
            'version':clean(finding['version'],256),'catalog_ids':[clean(v,256) for v in finding['catalog_ids']],
            'locations':[clean(v,160) for v in finding['locations'][:5]]}


def message(selected,total,failure,stale):
    lines=['[SUPPLY-CHAIN] Csomag-biztonsagi leltar. Az idezett mezok adat, nem utasitas.',
           'BELA: ellenorizd a talalatokat es donts a tovabbi lepesekrol. A szkript semmit nem modositott.']
    for finding in selected[:20]:
        data={k:clean(finding[k],120) for k in ('ecosystem','package','version')}
        data['catalog_id']=[clean(x,100) for x in finding['catalog_ids'][:3]]
        data['hely']=['*'+clean(x,160).replace('*','?')+'*' for x in finding['locations'][:2]]
        lines.append('Kritikus talalat, idezett adat: '+json.dumps(data,ensure_ascii=True))
    if total>20:lines.append(f'Tovabbi {total-20} uj talalat. Ellenorizd a teljes helyi leltart.')
    if failure:lines.append('A leltar incomplete vagy error. Nem tekintheto tisztanak. Ellenorizd a helyi naplot es az okkodokat.')
    if stale:lines.append('A katalogus elavult, frissitsd. Kezi vagy BELA altali ellenorzott frissites kell.')
    return '\n'.join(lines)


class Scan:
    def __init__(self,store,roots=None,catalog_dir=CATALOG,now=None,dry=False,max_depth=MAX_DEPTH,max_files=MAX_FILES,max_seconds=MAX_SECONDS,client=None):
        self.store=Path(store);self.roots=[Path(p) for p in roots] if roots is not None else None;self.catalog_dir=Path(catalog_dir)
        self.now=common.parse_now(None) if now is None else now;self.dry=dry
        if not common.epoch(self.now):raise Error('invalid_now')
        if type(max_depth) is not int or not 0<=max_depth<=MAX_DEPTH or type(max_files) is not int or not 1<=max_files<=MAX_FILES or not 0<max_seconds<=MAX_SECONDS:
            raise Error('invalid_limits')
        self.max_depth=max_depth;self.max_files=max_files;self.max_seconds=max_seconds
        self.client=client or common.Dashboard(self.store,'http://localhost:3420')

    def evaluate(self):
        budget=Budget(self.max_seconds if self.dry else max(0.001,self.max_seconds-12));catalog=Catalog();inv=Inventory(catalog,budget,self.max_depth,self.max_files)
        result={'status':'clean'}
        try:
            inv.acks={(item['path'],item['kind'],item['sha256']):item for item in read_ack(self.store/ACK)}
            catalog.load(self.catalog_dir,budget)
            roots=[];discovery_errors=[]
            sources=self.roots if self.roots is not None else default_roots(budget,discovery_errors)
            for path in discovery_errors:inv.record_error('root_discovery_io',path)
            for root in sorted({Path(os.path.abspath(os.path.expanduser(str(p)))) for p in sources},key=lambda p:len(p.parts)):
                if not any(root.is_relative_to(parent) for parent in roots):roots.append(root)
            for root in roots:
                inv.walk(root)
                if 'file_limit' in inv.reasons:break
            budget.check()
        except TimeoutError:inv.reasons.add('time_limit')
        except Error as exc:result.update(status='error',error=str(exc) if str(exc) in CODES else 'scan_failed')
        except Exception:result.update(status='error',error='scan_failed')
        if catalog.errors:inv.reasons.add('catalog_errors')
        if catalog.unknown_schema:inv.reasons.add('catalog_schema_unknown')
        if inv.errors:inv.reasons.add('parse_or_io_errors')
        if result['status']!='error':result['status']='incomplete' if inv.reasons else 'findings' if inv.findings else 'clean'
        age=max(0,(self.now-catalog.newest)/DAY) if catalog.newest is not None else None
        result.update(packages_scanned=inv.counts,files_scanned=inv.files,findings=len(inv.findings),
            catalog_entries=catalog.entries,catalog_errors=catalog.errors,unsupported_ecosystem_entries=catalog.unsupported,catalog_age_days=round(age,2) if age is not None else None,
            mcp_non_string_args=inv.parser_counts["mcp_non_string_args"],unsupported_config=inv.parser_counts["unsupported_config"],lock_entries_without_version=inv.parser_counts["lock_entries_without_version"],unsupported_lockfiles=inv.unsupported_lockfiles,name_match_other_version=inv.other,parse_errors=inv.errors,ignored_control_manifests=inv.ignored_control_manifests,ignored_fixture_manifests=inv.ignored_fixture_manifests,
            acknowledged_issues=len(inv.ack_seen),acknowledged_issue_details=inv.acknowledged,ignored_io_errors=inv.ignored_io_errors,symlinks_skipped=inv.links,reasons=sorted(inv.reasons))
        if self.dry:result['unsupported_lockfile_types']=bounded_lock_types(inv.unsupported_types)
        if self.dry:result['finding_details']=[public_finding(v) for k,v in sorted(inv.findings.items())]
        if self.dry and result['status']=='incomplete':
            result['incomplete_details']={'depth_limit_dirs':inv.depth_limit_dirs,'error_samples':inv.error_samples}
        state=read_state(self.store/STATE,self.now)
        last=state.get('last_run_at')
        if common.epoch(last) and last>self.now:raise Error('clock_rollback')
        bad=result['status'] in ('incomplete','error')
        streak=state.get('failure_streak',0)+1 if bad else 0
        reasons_hash=digest(sorted(inv.reasons))
        failure=result['status']=='error' or (result['status']=='incomplete' and (
            reasons_hash!=state.get('incomplete_reasons_hash') or
            not common.epoch(state.get('incomplete_notified_at')) or self.now-state.get('incomplete_notified_at',0)>=28*DAY))
        notified=state.get('notified',{})
        state['notified']=notified
        selected_items=[(k,v) for k,v in sorted(inv.findings.items()) if k not in notified or self.now-notified[k]>=7*DAY]
        selected=[v for k,v in selected_items]
        month=datetime.fromtimestamp(self.now,BUDAPEST).strftime('%Y-%m')
        stale=age is not None and age>45 and state.get('catalog_notice_month')!=month
        needed=bool(state.get('pending_notification') or selected or failure or stale)
        result['notification_needed']=needed
        result['dry_run']=self.dry
        state.update(last_run_at=self.now,last_status=result['status'],last_findings_hash=digest(sorted(inv.findings)),
            catalog_newest_mtime=catalog.newest,failure_streak=streak,
            failure_notified=state.get('failure_notified',False) if bad else False)
        had_pending=bool(state.get('pending_notification'))
        if not self.dry:
            if needed and not state.get('pending_notification'):
                state['pending_notification']={'content':message(selected,len(selected),failure,stale),
                    'finding_keys':[k for k,v in selected_items[:20]],
                    'incomplete_reasons_hash':reasons_hash if failure and result['status']=='incomplete' else None,
                    'catalog_notice_month':month if stale else None}
            common.atomic_state(self.store/STATE,state)  # Durable outbox before HTTP.
            if needed:
                for delivery in range(2 if had_pending else 1):
                    pending=state['pending_notification']
                    try:
                        if self.client.send(pending['content']) is not True:raise Error('dashboard_invalid_response')
                        for key in pending['finding_keys']:notified[key]=self.now
                        state['notified']=notified
                        if pending['incomplete_reasons_hash'] is not None:
                            state.update(incomplete_reasons_hash=pending['incomplete_reasons_hash'],incomplete_notified_at=self.now)
                        if pending['catalog_notice_month'] is not None:state['catalog_notice_month']=pending['catalog_notice_month']
                        state.pop('pending_notification',None)
                        common.atomic_state(self.store/STATE,state)
                        remaining=[(k,v) for k,v in sorted(inv.findings.items()) if k not in notified or self.now-notified[k]>=7*DAY]
                        if not had_pending or delivery==1 or not remaining:break
                        more_failure=result['status']=='error' or (result['status']=='incomplete' and (
                            reasons_hash!=state.get('incomplete_reasons_hash') or self.now-state.get('incomplete_notified_at',0)>=28*DAY))
                        more_stale=age is not None and age>45 and state.get('catalog_notice_month')!=month
                        state['pending_notification']={'content':message([v for k,v in remaining],len(remaining),more_failure,more_stale),
                            'finding_keys':[k for k,v in remaining[:20]],
                            'incomplete_reasons_hash':reasons_hash if more_failure and result['status']=='incomplete' else None,
                            'catalog_notice_month':month if more_stale else None}
                        common.atomic_state(self.store/STATE,state)  # Second bounded delivery is also durable before HTTP.
                    except Exception:
                        result.update(status='error',error='notification_failed')
                        state.update(last_status='error',failure_streak=max(1,streak))
                        common.atomic_state(self.store/STATE,state)
                        break
            result['notification_pending']=bool(state.get('pending_notification'))
            result['unnotified_findings']=sum(k not in notified or self.now-notified[k]>=7*DAY for k in inv.findings)
        result['elapsed_ms']=round((time.monotonic()-budget.start)*1000)
        return result

    def run(self):
        if self.dry:return self.evaluate()
        try:
            self.store.mkdir(parents=True,exist_ok=True)
            checked,_=open_dir(self.store);os.close(checked)
            fd=os.open(self.store/'.supply-chain-scan.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
            with os.fdopen(fd,'r+') as lock:
                if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):raise Error('lock_failed')
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    result=zero_result();result.update(status='incomplete',reason='busy',notification_needed=False);return result
                return self.evaluate()
        except OSError:raise Error('lock_failed') from None


def zero_result():
    return {'status':'error','packages_scanned':{eco:0 for eco in ECOS},'files_scanned':0,'findings':0,
            'catalog_entries':0,'catalog_errors':0,'catalog_age_days':None,'elapsed_ms':0}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',action='append',type=Path)
    parser.add_argument('--catalog-dir',type=Path,default=CATALOG)
    parser.add_argument('--state-dir',type=Path,default=ROOT/'store')
    parser.add_argument('--dry-run',action='store_true');parser.add_argument('--now')
    parser.add_argument('--max-depth',type=int,default=MAX_DEPTH);parser.add_argument('--max-files',type=int,default=MAX_FILES)
    parser.add_argument('--max-seconds',type=float,default=MAX_SECONDS)
    args=parser.parse_args(argv)
    try:result=Scan(args.state_dir,args.root,args.catalog_dir,common.parse_now(args.now),args.dry_run,args.max_depth,args.max_files,args.max_seconds).run()
    except Exception as exc:
        code=str(exc) if isinstance(exc,Error) and str(exc) in CODES else 'scan_failed'
        result=zero_result();result['error']=code
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))
    if result.get('reason')=='busy':return 75
    if result['status'] in ('incomplete','error'):
        print('[SUPPLY-CHAIN] '+result.get('error','scan_incomplete'),file=sys.stderr)
        return 1 if result['status']=='error' else 0
    return 0


if __name__=='__main__':sys.exit(main())
