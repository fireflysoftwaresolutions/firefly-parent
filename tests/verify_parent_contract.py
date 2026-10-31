#!/usr/bin/env python3
"""Bounded file-only regression: no install, deploy, service, or shared Maven cache."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as E
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'e4d08bd4201e26c0054a8a7cf17b2b0f21382903'
MAVEN_VERSION = '3.9.16'
JAVA_VERSION = '25.0.3'
NS = {'m': 'http://maven.apache.org/POM/4.0.0'}
OWNER = 'parent-flatten-contract-v1'


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def save(p, v):
    p.write_text(json.dumps(v, indent=2, sort_keys=True) + '\n')


def size(p):
    return sum(f.stat().st_size for f in p.rglob('*') if f.is_file())


def resolve_tools(maven=None, java_home=None, environ=None, platform=None):
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform not in ('darwin', 'linux'):
        raise ValueError('Unsupported platform: '+platform+'; this process-group runner supports macOS and Linux')
    choice = str(Path(maven or environ.get('MAVEN_EXECUTABLE') or 'mvn').expanduser())
    executable = shutil.which(choice, path=environ.get('PATH', os.defpath))
    if executable is None:
        raise ValueError('Maven executable not found; set --maven or MAVEN_EXECUTABLE/PATH')
    maven = Path(executable).resolve(strict=True)
    home = java_home or environ.get('JAVA_HOME')
    if not home:
        raise ValueError('Set --java-home or JAVA_HOME; no JDK location is guessed or installed')
    java = Path(home).expanduser().resolve(strict=True)
    for name in ['java','javac']:
        if not (java/'bin'/name).is_file() or not os.access(java/'bin'/name, os.X_OK):
            raise ValueError('Java home must contain an executable bin/'+name)
    release = java/'release'
    version = re.search(r'^JAVA_VERSION="([^"]+)"', release.read_text(), re.MULTILINE)
    if version is None or version.group(1) != JAVA_VERSION:
        raise ValueError('Java '+JAVA_VERSION+' is required by this qualified regression')
    library = java/'lib/server'/('libjvm.dylib' if platform=='darwin' else 'libjvm.so')
    if not library.is_file():
        raise ValueError('Unsupported JDK layout; missing '+str(library))
    roots = [maven.parent.parent, maven.parent.parent/'libexec']
    layouts = [(root, list((root/'lib').glob('maven-core-*.jar'))) for root in roots]
    layouts = [(root, jars) for root, jars in layouts if jars]
    if len(layouts) != 1 or len(layouts[0][1]) != 1:
        raise ValueError('Unsupported Maven layout; expected one distribution maven-core JAR')
    maven_home, cores = layouts[0]
    with zipfile.ZipFile(cores[0]) as jar:
        properties = jar.read('META-INF/maven/org.apache.maven/maven-core/pom.properties').decode()
    version = re.search(r'^version=(.+)$', properties, re.MULTILINE)
    if version is None or version.group(1).strip() != MAVEN_VERSION:
        raise ValueError('Maven '+MAVEN_VERSION+' is required by this qualified regression')
    return {'maven':str(maven),'mavenHome':str(maven_home.resolve()),'javaHome':str(java),'jvmLibrary':str(library.resolve()),'mavenVersion':MAVEN_VERSION,'javaVersion':JAVA_VERSION,'platform':platform}


def pin_tools(tools):
    java, maven = Path(tools['javaHome']), Path(tools['mavenHome'])
    files = [Path(tools['maven']), java/'bin/java', java/'bin/javac', java/'release', Path(tools['jvmLibrary'])]
    files += sorted(maven.glob('lib/*.jar')) + sorted(maven.glob('boot/*.jar'))
    return {str(f.resolve()): sha(f) for f in files}


def maven_command(out, args, tools):
    return [tools['maven'], '-B', '-ntp', '-s', str(out/'settings.xml'), '-gs', str(out/'settings.xml'), '-Dmaven.repo.local='+str(out/'cache'), *args]


def maven_environment(tools):
    env = os.environ.copy()
    for key in ['MAVEN_ARGS','MAVEN_OPTS','JAVA_TOOL_OPTIONS','JDK_JAVA_OPTIONS']:
        env.pop(key, None)
    env.update(JAVA_HOME=tools['javaHome'], MAVEN_SKIP_RC='true')
    return env


def finish_owned(child):
    result = {'pid':child.pid,'signals':[],'errors':[],'returnCode':None}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        running = False
        try:
            code = child.poll()
            if code is not None:
                result['returnCode'] = code
                break
            running = True
        except BaseException as error:
            result['errors'].append('poll: '+repr(error))
        if running:
            try:
                # An unreaped Popen child plus its new session/group pins this identity.
                if os.getpgid(child.pid) == child.pid and os.getsid(child.pid) == child.pid:
                    os.killpg(child.pid, sig)
                    result['signals'].append(sig.name)
                else:
                    result['errors'].append('Unverified owned process group/session; no signal sent')
            except BaseException as error:
                result['errors'].append(sig.name+': '+repr(error))
        try:
            result['returnCode'] = child.wait(timeout=5)
            break
        except BaseException as error:
            result['errors'].append('wait after '+sig.name+': '+repr(error))
    try:
        result['returnCode'] = child.wait(timeout=5)
    except BaseException as error:
        result['errors'].append('final reap: '+repr(error))
    return result


def run(out, phase, label, args, tools):
    cmd = maven_command(out, args, tools)
    save(phase/(label+'-command.json'), {'argv':cmd,'cwd':str(phase),'timeoutSeconds':240})
    env = maven_environment(tools)
    start = time.monotonic()
    log = phase/(label+'.log')
    reason = None
    primary = None
    code = None
    cleanup = None
    evidence_errors = []
    with log.open('wb') as stream:
        child = None
        try:
            child = subprocess.Popen(cmd,cwd=phase,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            save(phase/(label+'-process.json'),{'pid':child.pid,'pgid':child.pid,'commandSha256':sha(phase/(label+'-command.json'))})
            while child.poll() is None:
                if time.monotonic()-start>240 or size(out/'cache')>900*1024**2 or size(out)>1900*1024**2:
                    reason = 'Owned deadline or conservative cache/output budget reached'
                    break
                time.sleep(.2)
            if reason is None:
                code = child.wait(timeout=5)
        except BaseException as error:
            primary = error
        finally:
            if child is not None:
                cleanup = finish_owned(child)
                if code is None:
                    code = cleanup['returnCode']
            result = {'exitCode':code,'stopReason':reason,'cleanup':cleanup,'evidenceErrors':evidence_errors}
            if primary is not None:
                result['primaryFailure'] = repr(primary)
            for key, read in [('seconds',lambda:round(time.monotonic()-start,3)),('logSha256',lambda:sha(log)),('cacheBytes',lambda:size(out/'cache')),('outputBytes',lambda:size(out))]:
                try:
                    result[key] = read()
                except BaseException as error:
                    evidence_errors.append(key+': '+repr(error))
            try:
                save(phase/(label+'-result.json'),result)
            except BaseException as error:
                evidence_errors.append('result save: '+repr(error))
    if primary is not None:
        if evidence_errors or (cleanup and cleanup['errors']):
            primary.add_note('Cleanup/evidence observations: '+repr({'cleanup':cleanup,'evidenceErrors':evidence_errors}))
        raise primary
    if evidence_errors or (cleanup and cleanup['errors']):
        raise RuntimeError('Owned cleanup/evidence failed: '+repr({'cleanup':cleanup,'evidenceErrors':evidence_errors}))
    assert reason is None, reason
    return code, log.read_text(errors='replace')


def pom(parent_group, parent_artifact, parent_version, relative, artifact):
    return f'''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<parent><groupId>{parent_group}</groupId><artifactId>{parent_artifact}</artifactId><version>{parent_version}</version><relativePath>{relative}</relativePath></parent>
<groupId>io.fireflysoftware.qualification</groupId><artifactId>{artifact}</artifactId><version>0.0.0-qualification</version><packaging>pom</packaging>
<dependencies><dependency><groupId>org.fireflyframework</groupId><artifactId>fireflyframework-r2dbc</artifactId></dependency><dependency><groupId>org.postgresql</groupId><artifactId>postgresql</artifactId></dependency></dependencies>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId></plugin><plugin><groupId>org.openapitools</groupId><artifactId>openapi-generator-maven-plugin</artifactId></plugin></plugins></build></project>\n'''


def plugin(model, artifact, managed=False):
    path = 'm:build/'+('m:pluginManagement/' if managed else '')+'m:plugins/m:plugin'
    return next(x for x in model.findall(path,NS) if x.findtext('m:artifactId',namespaces=NS)==artifact)


def assert_consumer(path):
    m=E.parse(path).getroot()
    deps={x.findtext('m:artifactId',namespaces=NS):x.findtext('m:version',namespaces=NS) for x in m.findall('m:dependencies/m:dependency',NS)}
    assert deps['fireflyframework-r2dbc']=='26.10.20'
    assert deps['postgresql']=='42.7.14'
    for artifact,version in [('maven-compiler-plugin','3.15.0'),('openapi-generator-maven-plugin','7.26.0')]:
        assert plugin(m,artifact).findtext('m:version',namespaces=NS)==version
    assert plugin(m,'maven-surefire-plugin',True).findtext('m:version',namespaces=NS)=='3.6.0'
    assert plugin(m,'flatten-maven-plugin').findtext('m:configuration/m:flattenMode',namespaces=NS)=='resolveCiFriendliesOnly'
    assert m.findtext('m:properties/m:base.package',namespaces=NS)=='io.fireflysoftware'
    assert m.findtext('m:properties/m:fireflyframework.bom.version',namespaces=NS)=='26.10.20'
    managed={x.findtext('m:artifactId',namespaces=NS):x.findtext('m:version',namespaces=NS) for x in m.findall('m:dependencyManagement/m:dependencies/m:dependency',NS)}
    assert managed['core-banking-accounts-sdk']=='26.10.6'


def verify(out,name,tools):
    phase=out/name
    phase.mkdir()
    parent=phase/'parent'
    parent.mkdir()
    raw=subprocess.check_output(['git','show',BASELINE+':pom.xml'],cwd=ROOT) if name=='baseline' else (ROOT/'pom.xml').read_bytes()
    (parent/'pom.xml').write_bytes(raw)
    mode=E.fromstring(raw).findtext("m:profiles/m:profile[m:id='release']/m:build/m:plugins/m:plugin[m:artifactId='flatten-maven-plugin']/m:configuration/m:flattenMode",namespaces=NS)
    assert mode==('ossrh' if name=='baseline' else 'resolveCiFriendliesOnly')
    save(phase/'source-input.json',{'baseline':BASELINE,'sourceSha256':sha(parent/'pom.xml'),'mode':mode,'worktree':str(ROOT)})
    flatten=['-Prelease','org.codehaus.mojo:flatten-maven-plugin:1.7.0:flatten']
    code,_=run(out,phase,'flatten-parent',['-f',str(parent/'pom.xml'),*flatten],tools)
    assert code==0,'Original flatten goal failed; preserve and inspect log'
    artifact=parent/'.flattened-pom.xml'
    assert artifact.is_file()
    consumer=phase/'consumer'
    consumer.mkdir()
    (consumer/'pom.xml').write_text(pom('io.fireflysoftware','firefly-parent','26.10.6','../parent/.flattened-pom.xml','flatten-parent-consumer'))
    def effective(folder,label):
        return run(out,phase,label,['-f',str(folder/'pom.xml'),'-Prelease','org.apache.maven.plugins:maven-help-plugin:3.5.2:effective-pom','-Doutput='+str(folder/'effective-pom.xml')],tools)
    code,log=effective(consumer,'consumer-effective-model')
    result={'phase':name,'flattenedParentSha256':sha(artifact),'consumerUsesExplicitRelativePath':True,'consumerExitCode':code}
    if name=='baseline':
        assert code!=0 and 'dependencies.dependency.version' in log and 'fireflyframework-r2dbc' in log and 'postgresql' in log,'RED must be the diagnosed version-management failure'
        result['result']='RED_REPRODUCED_MISSING_MANAGED_VERSIONS'
    else:
        assert code==0,'Corrected consumer model failed; preserve and inspect log'
        assert_consumer(consumer/'effective-pom.xml')
        code,_=run(out,phase,'flatten-descendant',['-f',str(consumer/'pom.xml'),*flatten],tools)
        assert code==0
        descendant=phase/'descendant-consumer'
        descendant.mkdir()
        (descendant/'pom.xml').write_text(pom('io.fireflysoftware.qualification','flatten-parent-consumer','0.0.0-qualification','../consumer/.flattened-pom.xml','flatten-descendant-consumer'))
        code,_=effective(descendant,'descendant-effective-model')
        assert code==0
        assert_consumer(descendant/'effective-pom.xml')
        result.update(result='GREEN_PARENT_AND_FLATTENED_DESCENDANT_CONSUMERS',effectiveParentConsumerSha256=sha(consumer/'effective-pom.xml'),flattenedDescendantSha256=sha(consumer/'.flattened-pom.xml'),effectiveDescendantConsumerSha256=sha(descendant/'effective-pom.xml'))
    save(phase/'assertions.json',result)
    return result


def parse_options(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--phase',choices=['baseline','candidate','all'],default='all')
    parser.add_argument('--maven',help='Maven executable path/name; defaults to MAVEN_EXECUTABLE or mvn on PATH')
    parser.add_argument('--java-home',type=Path,help='JDK home; defaults to JAVA_HOME')
    return parser.parse_args(argv)


def main():
    args=parse_options()
    tools=resolve_tools(args.maven,args.java_home)
    out=args.output.resolve()
    if out.exists():
        assert args.phase=='candidate' and json.loads((out/'owner.json').read_text())['owner']==OWNER
        assert (out/'baseline/assertions.json').is_file()
        assert (out/'tool-resolution.json').is_file(), 'Choose a fresh output root for the portable tool contract'
        assert json.loads((out/'tool-resolution.json').read_text())==tools
    else:
        assert args.phase in ['baseline','all']
        out.mkdir(parents=True)
        save(out/'owner.json',{'owner':OWNER,'pid':os.getpid(),'worktree':str(ROOT)})
        (out/'cache').mkdir()
        (out/'settings.xml').write_text('<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"><mirrors><mirror><id>central-only</id><mirrorOf>*</mirrorOf><url>https://repo.maven.apache.org/maven2</url></mirror></mirrors><interactiveMode>false</interactiveMode></settings>\n')
        save(out/'tool-resolution.json',tools)
        save(out/'tool-pins.json',pin_tools(tools))
    assert json.loads((out/'tool-pins.json').read_text())==pin_tools(tools)
    results=[verify(out,n,tools) for n in (['baseline','candidate'] if args.phase=='all' else [args.phase])]
    assert not list((out/'cache').glob('io/fireflysoftware/firefly-parent/*/*.pom')),'No parent GAV substitution/install'
    assert not list((out/'cache').glob('io/fireflysoftware/**/*.jar')),'No SDK/service binary acquisition'
    files={str(f.relative_to(out/'cache')):{'sha256':sha(f),'bytes':f.stat().st_size} for f in sorted((out/'cache').rglob('*')) if f.is_file()}
    save(out/'cache-origin-and-hash-inventory.json',{'onlyRepository':'https://repo.maven.apache.org/maven2','credentials':False,'cacheBytes':size(out/'cache'),'files':files})
    save(out/('result-'+args.phase+'.json'),{'results':results,'cacheBytes':size(out/'cache'),'outputBytes':size(out),'installDeployOrRuntimeServices':False})
    print(json.dumps({'results':results,'cacheBytes':size(out/'cache')}))


if __name__=='__main__':
    main()
