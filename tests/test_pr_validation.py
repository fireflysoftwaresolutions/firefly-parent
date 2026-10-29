"""Execute the workflow's Python with the real helper and controlled command boundaries."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('PR_VALIDATION_SOURCE_ROOT', Path(__file__).resolve().parents[1]))
TOOLS = Path(os.environ['RELEASE_TOOLS_ROOT']).resolve()
SHA = '7b046ce67239bf889ab68e380614ba34723c9392'
SPEC = importlib.util.spec_from_file_location('pr_validation_helper', TOOLS/'scripts/java_release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def workflow():
    path = ROOT/'.github/workflows/pr-validation.yml'
    if not path.is_file():
        raise AssertionError('Read-only PR validation workflow is missing')
    return json.loads(subprocess.check_output(['ruby','-rjson','-ryaml','-e',
        'puts JSON.generate(YAML.safe_load(File.read(ARGV[0])))',str(path)], text=True))


class PrValidationControls(unittest.TestCase):
    @contextlib.contextmanager
    def invoke(self, mode='success'):
        flow = workflow()
        job, = flow['jobs'].values()
        step = next(s for s in job['steps'] if s.get('id') == 'validate')
        code = step['run'].split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace/'source'; source.mkdir()
            (workspace/'release-tools').symlink_to(TOOLS, target_is_directory=True)
            (workspace/'temp').mkdir()
            (source/'.github').mkdir()
            pom = '<project><groupId>org.example</groupId><artifactId>qualification</artifactId><version>26.10.6</version><packaging>pom</packaging></project>'
            (source/'pom.xml').write_text(pom)
            policy = {'schema':1,'kind':'pom','profiles':['release'],'suites':[], 'minimum_tests':0,
                'artifacts':[{'groupId':'org.example','artifactId':'qualification','version':'26.10.6','packaging':'pom','files':{'.pom':'pom.xml'}}]}
            (source/'.github/release-policy.json').write_text(json.dumps(policy))
            if mode.startswith('launcher:'):
                (source/'.mvn').mkdir()
                (source/'.mvn'/mode.split(':',1)[1]).write_text('requires independent launcher review')
            calls = []
            original_command = release.command

            def command(args, **kwargs):
                args = list(map(str,args)); calls.append(args)
                if args == ['mvn','--version']:
                    return subprocess.CompletedProcess(args,0,stdout='Apache Maven 3.9.16\n',stderr='')
                if args == ['java','-version']:
                    return subprocess.CompletedProcess(args,0,stdout='',stderr='openjdk version \"25.0.3\"\n')
                if args[:2] == ['git','ls-files'] and Path(kwargs['cwd']) == source:
                    return subprocess.CompletedProcess(args,0,stdout=b'.github/release-policy.json\0pom.xml\0')
                if args[:3] == ['git','rev-parse','HEAD'] and Path(kwargs['cwd']) == source:
                    return subprocess.CompletedProcess(args,0,stdout='a'*40+'\n')
                if args[:3] == ['git','status','--porcelain'] and Path(kwargs['cwd']) == source:
                    return subprocess.CompletedProcess(args,0,stdout='')
                if args[0] == 'mvn' or '-m' in args and 'unittest' in args:
                    if 'stdout' in kwargs:
                        kwargs['stdout'].write(b'Controlled ordinary command boundary; private output.\n')
                    if args[0] != 'mvn':
                        if mode == 'unit_failure': raise subprocess.CalledProcessError(9,args)
                    else:
                        self.assertIn('-Prelease',args)
                        self.assertIn('clean',args); self.assertIn('verify',args)
                        self.assertIn('org.apache.maven.plugins:maven-help-plugin:3.5.1:effective-pom',args)
                        self.assertNotIn('install',args); self.assertNotIn('deploy',args)
                        cache = Path(next(a.split('=',1)[1] for a in args if a.startswith('-Dmaven.repo.local=')))
                        self.assertEqual(list(cache.iterdir()),[])
                        settings = Path(args[args.index('-s')+1])
                        self.assertEqual(args[args.index('-gs')+1],str(settings))
                        self.assertNotIn('<servers>',settings.read_text())
                        if mode == 'maven_failure': raise subprocess.CalledProcessError(9,args)
                        effective = Path(next(a.split('=',1)[1] for a in args if a.startswith('-Doutput=')))
                        effective.write_text(pom.replace('qualification','other') if mode == 'model_mismatch' else pom)
                        if mode == 'source_change': (source/'pom.xml').write_text(pom+'\n')
                    return subprocess.CompletedProcess(args,0)
                if args[:3] == ['git','rev-parse','HEAD'] and Path(kwargs['cwd']).resolve() == TOOLS:
                    return original_command(args,**kwargs)
                raise AssertionError('Unsupported command in read-only orchestration: '+args[0])

            env = {'GITHUB_WORKSPACE':str(workspace),'RUNNER_TEMP':str(workspace/'temp'),
                'GITHUB_SHA':'a'*40,'GITHUB_REPOSITORY':'org.example/qualification',
                'RELEASE_TOOLS_ROOT':str(TOOLS),'PYTHONDONTWRITEBYTECODE':'1'}
            before = list(sys.path)
            status = 0
            try:
                with patch.object(release,'command',side_effect=command), patch.dict(sys.modules,{'java_release':release}), patch.dict(os.environ,env), contextlib.redirect_stdout(io.StringIO()):
                    try: exec(compile(code,'workflow-pr-validation','exec'),{'__name__':'__main__'})
                    except SystemExit as error: status = error.code
            finally:
                sys.path[:] = before
            yield status,workspace/'temp/java-pr-validation-evidence',calls

    def test_extracted_orchestration_freezes_exact_bytes_and_closed_provenance(self):
        with self.invoke() as (status,evidence,calls):
            self.assertEqual(status,0)
            proof = json.loads((evidence/'validation-proof.json').read_text())
            self.assertEqual(set(proof),{'schema','repository','source_sha','workflow_sha','tools','helper_sha256','policy_sha256','source_snapshot_sha256','ordinary_controls_log_sha256','normal_build_log_sha256','effective_pom_sha256','projects','tests','artifacts','publication'})
            self.assertEqual(proof['source_sha'],'a'*40)
            self.assertEqual(proof['workflow_sha'],SHA)
            self.assertIn('tools',proof)
            self.assertEqual(proof['tools']['java_version'],'25.0.3')
            self.assertEqual(proof['tools']['maven_version'],'3.9.16')
            self.assertFalse(proof['publication'])
            self.assertEqual(proof['tests']['tests'],0)
            self.assertEqual(len(proof['artifacts']),1)
            self.assertEqual(proof['artifacts'][0]['source_path'],'pom.xml')
            self.assertEqual(len(proof['source_snapshot_sha256']),64)
            self.assertLess((evidence/'validation-proof.json').stat().st_size,131072)
            self.assertEqual(sum(args[0]=='mvn' and 'clean' in args for args in calls),1)
            self.assertEqual(sum('unittest' in args for args in calls),1)

    def assert_failure(self,mode,phase,exit_code):
        with self.invoke(mode) as (status,evidence,calls):
            self.assertEqual(status,exit_code)
            self.assertFalse((evidence/'validation-proof.json').exists())
            failure = json.loads((evidence/'validation-failure.json').read_text())
            self.assertEqual(failure['phase'],phase)
            self.assertEqual(failure['exit_code'],exit_code if exit_code==9 else None)
            self.assertEqual(set(failure),{'schema','phase','exit_code','error_kind','error_sha256','normal_build_log_sha256','effective_pom_sha256','original_sdk_proof_sha256'})
            self.assertNotIn('private output',(evidence/'validation-failure.json').read_text())
            return calls

    def test_existing_ordinary_control_failure_preserves_exit_and_stops_before_maven_build(self):
        calls = self.assert_failure('unit_failure','TEST_EVIDENCE',9)
        self.assertFalse(any(args[0]=='mvn' and 'clean' in args for args in calls))

    def test_normal_build_failure_preserves_original_exit_with_closed_metadata(self):
        self.assert_failure('maven_failure','COLD_BUILD',9)

    def test_tracked_source_change_refuses_before_freezing(self):
        self.assert_failure('source_change','EFFECTIVE_POM',1)

    def test_actual_effective_model_must_match_reviewed_policy(self):
        self.assert_failure('model_mismatch','FREEZE',1)

    def test_nonempty_launcher_configuration_requires_separate_review(self):
        for name in ('maven.config','jvm.config','extensions.xml'):
            with self.subTest(file=name):
                calls = self.assert_failure('launcher:'+name,'TEST_EVIDENCE',1)
                self.assertFalse(any(args[0]=='mvn' and 'clean' in args for args in calls))

    def test_workflow_has_read_only_authority_and_exact_json_upload_paths(self):
        flow = workflow(); self.assertIn('pull_request',flow['true'])
        self.assertNotIn('pull_request_target',flow['true'])
        self.assertEqual(flow['permissions'],{'contents':'read'})
        job, = flow['jobs'].values()
        self.assertNotIn('secrets',job)
        self.assertNotIn('permissions',job)
        for step in job['steps']:
            if 'uses' in step:
                self.assertRegex(step['uses'],r'@[0-9a-f]{40}$')
                self.assertNotIn('cache',step.get('with',{}))
        checkouts = [s for s in job['steps'] if s.get('uses','').startswith('actions/checkout@')]
        self.assertEqual(len(checkouts),2)
        self.assertTrue(all(s['with']['persist-credentials'] is False for s in checkouts))
        tools = next(s for s in checkouts if s['with'].get('repository'))
        self.assertEqual(tools['with']['ref'],SHA)
        uploader = next(s for s in job['steps'] if s.get('uses','').startswith('actions/upload-artifact@'))
        self.assertEqual(uploader['with']['path'].splitlines(),[
            '${{ runner.temp }}/java-pr-validation-evidence/validation-proof.json',
            '${{ runner.temp }}/java-pr-validation-evidence/validation-failure.json'])


if __name__ == '__main__':
    unittest.main()
