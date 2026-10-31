"""Offline release caller controls, using the exact shared workflow implementation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHA = '7b046ce67239bf889ab68e380614ba34723c9392'
HELPER_SHA256 = '7698c8fec01da13db8f70e831fb207b6d7774a2f7d4cb36bccaeaa6f1228606d'
ARTIFACT = 'firefly-parent'
PUBLISH_POM = '.flattened-pom.xml'
TOOLS = Path(os.environ['RELEASE_TOOLS_ROOT']).resolve()
if subprocess.check_output(['git','rev-parse','HEAD'],cwd=TOOLS,text=True).strip() != SHA:
    raise RuntimeError('Use the reviewed immutable release-tools checkout')
for relative in ('scripts/java_release.py','.github/workflows/java-release.yml'):
    expected = subprocess.check_output(['git','show',SHA+':'+relative],cwd=TOOLS)
    if (TOOLS/relative).read_bytes() != expected:
        raise RuntimeError('Release-tools source differs from the immutable reviewed commit')
if hashlib.sha256((TOOLS/'scripts/java_release.py').read_bytes()).hexdigest() != HELPER_SHA256:
    raise RuntimeError('Release helper differs from the reviewed source digest')
SPEC = importlib.util.spec_from_file_location('java_release', TOOLS/'scripts/java_release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def yaml_file(path):
    return json.loads(subprocess.check_output(['ruby','-rjson','-ryaml','-e',
        'puts JSON.generate(YAML.safe_load(File.read(ARGV[0])))',str(path)],text=True))


class CallerPolicyControls(unittest.TestCase):
    def policy(self):
        path = ROOT/'.github/release-policy.json'
        self.assertTrue(path.is_file(), 'Reviewed repository release policy is missing')
        return json.loads(path.read_text())

    def project(self):
        return {'groupId':'io.fireflysoftware','artifactId':ARTIFACT,'version':'26.10.6','packaging':'pom'}

    def test_policy_freezes_complete_pom_reactor_bytes(self):
        policy = self.policy()
        release.validate_policy(policy)
        self.assertEqual(policy['profiles'], ['release'])
        self.assertEqual((policy['kind'],policy['suites'],policy['minimum_tests']), ('pom',[],0))
        self.assertEqual(policy['artifacts'], [dict(self.project(), files={'.pom':PUBLISH_POM})])
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source';source.mkdir()
            frozen=Path(folder)/'frozen';frozen.mkdir()
            publish=source/PUBLISH_POM
            publish.write_text('<project><groupId>io.fireflysoftware</groupId><artifactId>'+ARTIFACT+
                '</artifactId><version>26.10.6</version><packaging>pom</packaging></project>')
            rows=release.freeze(source,frozen,policy,[self.project()])
            self.assertEqual(len(rows),1)
            self.assertEqual((frozen/rows[0]['path']).read_bytes(),publish.read_bytes())

    def test_missing_publish_pom_refuses_freeze(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'Required regular file missing'):
                release.freeze(Path(folder),Path(folder)/'frozen',self.policy(),[self.project()])

    def test_mismatched_effective_version_refuses_freeze(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'publication order'):
                release.freeze(Path(folder),Path(folder)/'frozen',self.policy(),[dict(self.project(),version='26.10.7')])

    def test_non_pom_cannot_claim_exemption(self):
        policy=self.policy();policy['artifacts'][0]['packaging']='jar'
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'artifact inventory'):
                release.freeze(Path(folder),Path(folder)/'frozen',policy,[dict(self.project(),packaging='jar')])

    def test_tag_identity_accepts_counters_and_refuses_alias(self):
        for value in ('26.10.6','26.10.32','26.10.100'):
            release.check_tag('v'+value,value)
        with self.assertRaisesRegex(ValueError,'Tag differs'):
            release.check_tag('v26.10.06','26.10.6')

    def test_wrappers_satisfy_required_callee_inputs_and_identity(self):
        callee=yaml_file(TOOLS/'.github/workflows/java-release.yml')
        required={k for k,v in callee['true']['workflow_call']['inputs'].items() if v.get('required')}
        for name in ('release.yml','maven-central.yml'):
            with self.subTest(workflow=name):
                job,=yaml_file(ROOT/'.github/workflows'/name)['jobs'].values()
                self.assertEqual(job['uses'].rsplit('@',1)[1],SHA)
                self.assertTrue(required <= job['with'].keys())
                self.assertEqual(job['with']['release-tag'],'${{ github.ref_name }}')
                self.assertEqual(job['with']['release-policy'],'.github/release-policy.json')
                self.assertEqual(job['if'],"startsWith(github.ref, 'refs/tags/v')")
                self.assertEqual(job['permissions'],{'actions':'read','contents':'write','packages':'write'})

    def test_dispatch_and_tag_push_default_to_no_publication(self):
        for name in ('release.yml','maven-central.yml'):
            flow=yaml_file(ROOT/'.github/workflows'/name)
            dispatch=flow['true']['workflow_dispatch']
            self.assertIsInstance(dispatch, dict, 'Dispatch requires explicit dry-run input')
            inputs=dispatch['inputs']
            self.assertIs(inputs['dry-run']['default'],True)
            self.assertEqual(inputs['dry-run']['type'],'boolean')
            job,=flow['jobs'].values()
            self.assertEqual(job['with']['dry-run'],"${{ github.event_name == 'push' || inputs.dry-run }}")
            self.assertNotIn('release',flow['true'])

    def test_unreviewed_signing_key_refuses_central(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = self.policy()
            policy.pop('signing_fingerprint', None)
            with self.assertRaisesRegex(ValueError,'Reviewed full signing fingerprint'):
                release.central_bundle(Path(folder),[],Path(folder),policy,{})


    def test_central_uses_the_reviewed_prior_publisher_primary_fingerprint(self):
        policy = self.policy()
        self.assertEqual(policy.get('signing_fingerprint'), 'A89AED672B71B245F3E1C6260933FD1E179EF8A1')
        import base64
        import zipfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); frozen = root/'frozen'; frozen.mkdir()
            pom = frozen/'firefly-parent.pom'; pom.write_bytes(b'<project/>')
            calls = []
            def command(args, **kwargs):
                calls.append(args)
                if '--detach-sign' in args:
                    self.assertEqual(args[args.index('--local-user')+1], 'A89AED672B71B245F3E1C6260933FD1E179EF8A1')
                    Path(args[args.index('--output')+1]).write_bytes(b'SYNTHETIC-SIGNATURE-NOT-PROOF')
                return subprocess.CompletedProcess(args, 0)
            with patch.object(release, 'command', side_effect=command):
                bundle, extra = release.central_bundle(frozen, [{'path':pom.name}], root, policy,
                        {'GPG_PRIVATE_KEY':base64.b64encode(b'SYNTHETIC-NOT-A-KEY').decode(),
                         'MAVEN_GPG_PASSPHRASE':'SYNTHETIC-NOT-A-SECRET'})
            self.assertEqual([args[2] for args in calls], ['--import', '--pinentry-mode', '--verify'])
            self.assertEqual(len(extra), 5)
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(archive.read('firefly-parent.pom'), b'<project/>')


if __name__ == '__main__':
    unittest.main()
