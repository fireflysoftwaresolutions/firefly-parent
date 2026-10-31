"""Read-only Portal reconciliation refuses incomplete or ambiguous inventory."""
import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE / 'scripts/sonatype_deployments.py'
spec = importlib.util.spec_from_file_location('sonatype_deployments', SCRIPT)
m = importlib.util.module_from_spec(spec)
if SCRIPT.exists():
    spec.loader.exec_module(m)


def deployment(number=1, state='VALIDATED', version='26.10.22'):
    return {'deploymentId': f'deployment-{number}', 'deploymentName': 'PRIVATE-NAME',
            'namespace': 'io.fireflysoftware', 'deploymentState': state,
            'createTimestamp': '2026-10-31T10:00:00.000Z',
            'updateTimestamp': '2026-10-31T10:01:00.000Z',
            'deploymentComponents': [
                {'purl': f'pkg:maven/io.fireflysoftware/fireflyframework-parent@{version}',
                 'name': 'PRIVATE-FILENAME', 'path': 'PRIVATE-PATH',
                 'errors': ['PRIVATE-ERROR'], 'warnings': ['PRIVATE-WARNING']} ]}


def page(rows=None, number=0, total=None, size=2):
    rows = [deployment()] if rows is None else rows
    total = len(rows) if total is None else total
    return {'deployments': rows, 'page': number, 'pageSize': size,
            'pageCount': (total + size - 1) // size, 'totalResultCount': total}


TEST_URL = 'https://central.sonatype.com/api/v1/publisher/deployments?namespace=io.fireflysoftware&page=0&size=2&sortField=createTimestamp&sortDirection=asc'


class Response(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'application/json'}

    def __init__(self, payload, url):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.url = url

    def geturl(self):
        return self.url


class Api:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, Response) else Response(reply, request.full_url)


class PortalInventoryTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(m, 'collect'):
            self.fail('The read-only deployment inventory is not implemented.')

    def collect(self, replies):
        api = Api(replies)
        result = m.collect('TOKEN-SENTINEL', opener=api, page_size=2)
        return result, api

    def test_complete_inventory_has_exact_pending_gav_and_no_raw_fields(self):
        result, api = self.collect([page(), page()])
        self.assertEqual('ACCOUNT_VISIBLE_NAMESPACE_STABLE_TWO_SWEEPS', result['status'])
        self.assertEqual([{'groupId': 'io.fireflysoftware',
                          'artifactId': 'fireflyframework-parent', 'version': '26.10.22'}],
                         result['pendingDeployments'][0]['components'])
        self.assertEqual('VALIDATED', result['pendingDeployments'][0]['state'])
        self.assertFalse(result['versionAllocated'])
        self.assertFalse(result['atomicSnapshot'])
        self.assertNotIn('PRIVATE-', json.dumps(result))
        self.assertEqual(2, len(api.requests))
        for request, timeout in api.requests:
            self.assertEqual('GET', request.get_method())
            self.assertEqual('https://central.sonatype.com/api/v1/publisher/deployments?namespace=io.fireflysoftware&page=0&size=2&sortField=createTimestamp&sortDirection=asc', request.full_url)
            self.assertEqual('Bearer TOKEN-SENTINEL', request.get_header('Authorization'))
            self.assertLessEqual(timeout, 10)

    def test_multiple_pages_all_states_and_duplicate_files_collapse_to_one_gav(self):
        first = deployment(1, 'PUBLISHED')
        first['deploymentComponents'].append(copy.deepcopy(first['deploymentComponents'][0]))
        pages = [page([first, deployment(2, 'FAILED')], total=3),
                 page([deployment(3, 'VALIDATING')], number=1, total=3)]
        result, _ = self.collect(pages + copy.deepcopy(pages))
        self.assertEqual(3, result['deploymentCount'])
        self.assertEqual(2, len(result['pendingDeployments']))
        self.assertEqual(1, len(result['deployments'][0]['components']))

    def test_empty_namespace_is_explicit_authenticated_zero_not_a_404(self):
        for count in (0, 1):
            reply = page([]); reply['pageCount'] = count
            result, _ = self.collect([reply, copy.deepcopy(reply)])
            self.assertEqual([], result['pendingDeployments'])
            self.assertEqual(0, result['deploymentCount'])

    def test_auth_not_found_server_and_redirect_errors_never_become_empty(self):
        for code in (301, 302, 307, 308, 400, 401, 403, 404, 429, 500):
            error = urllib.error.HTTPError('https://central.sonatype.com', code,
                                           'TOKEN-SENTINEL PRIVATE-ERROR', {}, io.BytesIO(b'SECRET'))
            with self.subTest(code=code), self.assertRaises(m.InventoryError) as caught:
                self.collect([error])
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertNotIn('TOKEN', str(caught.exception))

    def test_unknown_pending_and_missing_or_empty_components_refuse(self):
        for state in ('PENDING', 'DELETING', '', None):
            with self.subTest(state=state), self.assertRaises(m.InventoryError):
                self.collect([page([deployment(state=state)])])
        for value in ([], None, {}, 'SECRET'):
            row = deployment(); row['deploymentComponents'] = value
            with self.subTest(value=value), self.assertRaises(m.InventoryError):
                self.collect([page([row])])

    def test_missing_duplicate_wrong_namespace_and_malformed_purls_refuse(self):
        for purl in ('pkg:maven/another/group@26.10.22', 'pkg:npm/foo@1',
                     'pkg:maven/io.fireflysoftware/a@',
                     'pkg:maven/io.fireflysoftware/a@26.10.22?repository_url=evil.example',
                     'pkg:maven/io.fireflysoftware/a@26.10.22%0Asecret'):
            row = deployment(); row['deploymentComponents'][0]['purl'] = purl
            with self.subTest(purl=purl), self.assertRaises(m.InventoryError):
                self.collect([page([row])])
        for key in ('deploymentId', 'namespace', 'deploymentState', 'deploymentComponents',
                    'createTimestamp', 'updateTimestamp'):
            row = deployment(); del row[key]
            with self.subTest(key=key), self.assertRaises(m.InventoryError):
                self.collect([page([row])])
        row = deployment(); row['namespace'] = 'another'
        with self.assertRaises(m.InventoryError): self.collect([page([row])])
        with self.assertRaises(m.InventoryError):
            self.collect([page([deployment(), deployment()], total=2)])

    def test_page_metadata_bool_negative_missing_wrong_page_and_counts_refuse(self):
        for key in ('page', 'pageSize', 'pageCount', 'totalResultCount'):
            for value in (True, -1, '1', None):
                reply = page(); reply[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(m.InventoryError):
                    self.collect([reply])
            reply = page(); del reply[key]
            with self.assertRaises(m.InventoryError): self.collect([reply])
        for patch_value in ({'page': 1}, {'pageSize': 0}, {'pageCount': 2},
                            {'totalResultCount': 2}, {'pageSize': 3}):
            reply = page(); reply.update(patch_value)
            with self.subTest(patch=patch_value), self.assertRaises(m.InventoryError):
                self.collect([reply])

    def test_shifted_total_between_pages_refuses(self):
        with self.assertRaises(m.InventoryError):
            self.collect([page([deployment(1), deployment(2)], total=3),
                          page([deployment(3)], number=1, total=4)])

    def test_repeated_sweep_change_in_state_component_or_timestamp_refuses(self):
        for field, value in (('deploymentState', 'FAILED'),
                             ('updateTimestamp', '2026-10-31T10:02:00Z'),
                             ('deploymentComponents', deployment(version='26.10.23')['deploymentComponents'])):
            second = deployment(); second[field] = value
            with self.subTest(field=field), self.assertRaises(m.InventoryError):
                self.collect([page(), page([second])])

    def test_caps_refuse_without_unbounded_pagination(self):
        reply = page(); reply.update(pageCount=101, totalResultCount=202)
        with self.assertRaises(m.InventoryError): self.collect([reply])
        row = deployment(); row['deploymentComponents'] *= 10001
        with self.assertRaises(m.InventoryError): self.collect([page([row])])

    def test_json_bytes_duplicate_keys_invalid_utf8_nonobject_and_body_cap_refuse(self):
        for body in (b'{"page":0,"page":1}', b'\xff', b'[]', b'null', b'{',
                     b' ' * (2 * 1024 * 1024 + 1)):
            api = Api([Response(body, TEST_URL)])
            with self.subTest(body=body[:20]), self.assertRaises(m.InventoryError):
                m.collect('TOKEN', opener=api, page_size=2)

    def test_official_host_no_redirect_and_json_content_type_are_mandatory(self):
        for url, content in (('https://evil.example/api', 'application/json'),
                             (TEST_URL, 'text/html')):
            response = Response(page(), url); response.headers = {'Content-Type': content}
            with self.assertRaises(m.InventoryError): self.collect([response])
        request = __import__('urllib.request', fromlist=['Request']).Request('https://central.sonatype.com')
        with self.assertRaises(m.InventoryError):
            m.NoRedirect().redirect_request(request, None, 302, 'SECRET', {}, 'https://evil.example')

    def test_transport_errors_are_constant_without_url_credentials_or_response(self):
        with self.assertRaises(m.InventoryError) as caught:
            self.collect([OSError('TOKEN-SENTINEL SECRET response')])
        self.assertEqual('Portal transport failed.', str(caught.exception))

    def test_token_pair_is_encoded_only_in_memory_and_missing_or_control_values_refuse(self):
        self.assertEqual(base64.b64encode(b'user:password').decode(), m.token('user', 'password'))
        for user, password in (('', 'p'), ('u', ''), ('u\n', 'p'), ('u:x', 'p')):
            with self.assertRaises(m.InventoryError): m.token(user, password)

    def test_outer_deadline_kills_worker_and_writes_only_safe_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('SECRET', 120)) as run:
                self.assertEqual(1, m.run_bounded(out))
            self.assertEqual(120, run.call_args.kwargs['timeout'])
            result = json.loads(out.read_text())
            self.assertEqual('INCOMPLETE', result['status'])
            self.assertFalse(result['versionAllocated'])
            self.assertNotIn('SECRET', out.read_text())

    def test_parent_readonly_workflow_binds_exact_source_and_scopes_credentials(self):
        path = SOURCE / '.github/workflows/sonatype-preflight.yml'
        workflow = json.loads(subprocess.check_output(['ruby', '-rjson', '-ryaml', '-e',
                'puts JSON.generate(YAML.safe_load(File.read(ARGV[0])))', str(path)], text=True))
        self.assertEqual(set(workflow['true']), {'workflow_dispatch'})
        self.assertEqual(workflow['permissions'], {'contents':'read'})
        job, = workflow['jobs'].values()
        self.assertEqual(job['if'], "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
        self.assertEqual(job['timeout-minutes'], 5)
        self.assertNotIn('env', job); self.assertNotIn('secrets', job)
        checkout, inventory, upload = job['steps']
        self.assertEqual(checkout['uses'], 'actions/checkout@11d5960a326750d5838078e36cf38b85af677262')
        self.assertEqual(checkout['with'], {'ref':'${{ github.sha }}', 'persist-credentials':False})
        self.assertEqual(inventory['run'], 'python3 -B scripts/sonatype_deployments.py --output sonatype-deployments.json')
        self.assertEqual(inventory['env'], {'SONATYPE_USERNAME':'${{ secrets.SONATYPE_USERNAME }}',
                                          'SONATYPE_PASSWORD':'${{ secrets.SONATYPE_PASSWORD }}'})
        self.assertEqual(upload['uses'], 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02')
        self.assertEqual(upload['if'], 'always()')
        self.assertEqual(upload['with']['path'], 'sonatype-deployments.json')
        self.assertEqual(upload['with']['retention-days'], 7)
        self.assertNotIn('env', checkout); self.assertNotIn('env', upload)

    def test_timeout_overwrites_a_worker_report_that_finished_after_its_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            def overrun(*args, **kwargs):
                out.write_text('{"status":"ACCOUNT_VISIBLE_NAMESPACE_STABLE_TWO_SWEEPS"}')
                raise subprocess.TimeoutExpired('SECRET', 120)
            with patch.object(m.subprocess, 'run', side_effect=overrun):
                self.assertEqual(1, m.run_bounded(out))
            self.assertEqual('INCOMPLETE', json.loads(out.read_text())['status'])
            self.assertEqual(0o600, out.stat().st_mode & 0o777)

    def test_cli_missing_credentials_refuses_without_network_or_secret_output(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            result = subprocess.run([os.sys.executable, '-B', str(SCRIPT), '--output', str(out)],
                                    env={'PATH': os.environ['PATH']}, capture_output=True, text=True, timeout=10)
            self.assertEqual(1, result.returncode)
            self.assertEqual('INCOMPLETE', json.loads(out.read_text())['status'])
            self.assertNotIn('Traceback', result.stderr)


class MavenPurlContractTests(unittest.TestCase):
    def project(self, purl):
        row = deployment(); row['deploymentComponents'][0]['purl'] = purl
        return m.project(row)['components']

    def code(self, purl):
        with self.assertRaises(m.InventoryError) as caught:
            self.project(purl)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        return getattr(caught.exception, 'reason_code', None)

    def test_maven_type_is_retained_without_changing_the_gav(self):
        result = self.project('pkg:maven/io.fireflysoftware/a@26.10.22?type=pom')
        self.assertEqual([{'groupId': 'io.fireflysoftware', 'artifactId': 'a', 'version': '26.10.22',
                           'qualifiers': {'type': 'pom'}}], result)

    def test_maven_classifier_is_retained_as_identity(self):
        result = self.project('pkg:maven/io.fireflysoftware/a@26.10.22?classifier=sources')
        self.assertEqual({'classifier': 'sources'}, result[0]['qualifiers'])

    def test_explicit_default_jar_is_canonical_with_the_plain_purl(self):
        self.assertEqual(self.project('pkg:maven/io.fireflysoftware/a@26.10.22'),
                         self.project('pkg:maven/io.fireflysoftware/a@26.10.22?type=jar'))

    def test_qualifier_order_and_one_encoding_pass_are_canonical(self):
        first = self.project('pkg:maven/io.fireflysoftware/a@26.10.22%2Bbuild?type=jar&classifier=%73ources')
        second = self.project('pkg:maven/io.fireflysoftware/a@26.10.22+build?classifier=sources')
        self.assertEqual(first, second)
        self.assertEqual('26.10.22+build', first[0]['version'])

    def test_same_gav_different_type_or_classifier_is_not_collapsed(self):
        row = deployment(); row['deploymentComponents'] = [
            {'purl': 'pkg:maven/io.fireflysoftware/a@26.10.22'},
            {'purl': 'pkg:maven/io.fireflysoftware/a@26.10.22?type=jar'},
            {'purl': 'pkg:maven/io.fireflysoftware/a@26.10.22?type=pom'},
            {'purl': 'pkg:maven/io.fireflysoftware/a@26.10.22?classifier=sources'}]
        result = m.project(row)['components']
        self.assertEqual(3, len(result))
        self.assertEqual(3, len({json.dumps(value, sort_keys=True) for value in result}))

    def test_classifier_change_between_sweeps_is_not_hidden_by_gav_projection(self):
        first = deployment(); second = deployment()
        first['deploymentComponents'][0]['purl'] += '?classifier=sources'
        second['deploymentComponents'][0]['purl'] += '?classifier=javadoc'
        api = Api([page([first]), page([second])])
        with self.assertRaises(m.InventoryError) as caught:
            m.collect('TOKEN', opener=api, page_size=2)
        self.assertEqual('Portal inventory changed between complete sweeps.', str(caught.exception))
        self.assertEqual(2, len(api.requests))

    def test_missing_purl_has_a_fixed_non_sensitive_classification(self):
        self.assertEqual('PURL_MISSING', self.code(None))

    def test_wrong_scheme_has_a_fixed_non_sensitive_classification(self):
        self.assertEqual('PURL_SCHEME', self.code('PRIVATE:maven/io.fireflysoftware/a@1'))

    def test_wrong_package_type_has_a_fixed_non_sensitive_classification(self):
        self.assertEqual('PURL_TYPE', self.code('pkg:PRIVATE/io.fireflysoftware/a@1'))

    def test_wrong_namespace_remains_refused_after_decoding(self):
        self.assertEqual('PURL_NAMESPACE', self.code('pkg:maven/io.fireflysoftware.PRIVATE/a@1'))
        self.assertEqual('PURL_NAMESPACE', self.code('pkg:maven/%69o.fireflysoftware.PRIVATE/a@1'))

    def test_subpaths_are_not_silently_discarded(self):
        self.assertEqual('PURL_SUBPATH', self.code('pkg:maven/io.fireflysoftware/a@1#PRIVATE'))

    def test_malformed_percent_double_encoding_and_encoded_separators_refuse(self):
        suffixes = ('%ZZ', '%252FPRIVATE', '%2FPRIVATE', '%0APRIVATE', '%FF', '%3FPRIVATE')
        self.assertEqual(['PURL_ENCODING'] * len(suffixes),
                         [self.code('pkg:maven/io.fireflysoftware/a@1' + value) for value in suffixes])

    def test_duplicate_qualifiers_cannot_hide_an_identity_collision(self):
        self.assertEqual('PURL_QUALIFIER_DUPLICATE', self.code(
            'pkg:maven/io.fireflysoftware/a@1?type=jar&type=pom'))
        self.assertEqual('PURL_QUALIFIER_DUPLICATE', self.code(
            'pkg:maven/io.fireflysoftware/a@1?type=jar&TYPE=pom'))
        self.assertEqual('PURL_QUALIFIER_DUPLICATE', self.code(
            'pkg:maven/io.fireflysoftware/a@1?classifier=sources&classifier=javadoc'))

    def test_unknown_or_malformed_qualifiers_are_not_stripped(self):
        queries = ('repository_url=PRIVATE', 'type=', 'classifier=x&PRIVATE=y', 'type=jar&', 'type=jar?PRIVATE')
        self.assertEqual(['PURL_QUALIFIER'] * len(queries),
                         [self.code('pkg:maven/io.fireflysoftware/a@1?' + query) for query in queries])

    def test_existing_ascii_artifact_version_profile_and_bounds_remain_strict(self):
        self.assertEqual('PURL_COORDINATES', self.code('pkg:maven/io.fireflysoftware/a@'))
        self.assertEqual('PURL_COORDINATES', self.code('pkg:maven/io.fireflysoftware/' + 'a' * 201 + '@1'))
        self.assertEqual('PURL_BOUNDS', self.code('pkg:maven/io.fireflysoftware/a@1?' + 'x' * 2048))

    def test_worker_report_contains_only_fixed_failure_code_not_the_purl(self):
        with self.assertRaises(m.InventoryError) as caught:
            self.project('pkg:maven/io.fireflysoftware/a@1#PRIVATE')
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            with patch.dict(os.environ, {'SONATYPE_USERNAME': 'synthetic', 'SONATYPE_PASSWORD': 'synthetic'}, clear=True), \
                 patch.object(m, 'collect', side_effect=caught.exception):
                self.assertEqual(1, m.worker(out))
            report = json.loads(out.read_text())
            self.assertEqual('INCOMPLETE', report['status'])
            self.assertEqual('PURL_SUBPATH', report['reasonCode'])
            self.assertNotIn('PRIVATE', out.read_text())
            self.assertFalse(report['versionAllocated'])


if __name__ == '__main__':
    unittest.main()
