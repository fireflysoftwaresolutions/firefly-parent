"""Status is read-only evidence, never an invented missing-row coordinate mapping."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error
import io
from test_sonatype_deployments import m, Api, Response, deployment, page

STATUS_URL = 'https://central.sonatype.com/api/v1/publisher/status?id=deployment-1'
PURL = 'pkg:maven/io.fireflysoftware/firefly-parent@26.10.6?type=pom'


def unknown():
    row = deployment()
    row['deploymentComponents'][0]['purl'] = None
    return row


def status(purls=None):
    return {'deploymentId': 'deployment-1', 'deploymentState': 'VALIDATED',
            'deploymentName': 'PRIVATE-NAME', 'purls': [PURL] if purls is None else purls,
            'errors': {'PRIVATE-KEY': ['PRIVATE-ERROR']}}


class StatusReconciliationTests(unittest.TestCase):
    def collect(self, rows=None, observation=None):
        rows = [unknown()] if rows is None else rows
        observation = status() if observation is None else observation
        api = Api([page(rows), observation, page(copy.deepcopy(rows)), copy.deepcopy(observation)])
        return m.collect('TOKEN-SENTINEL', opener=api, page_size=2), api

    def test_valid_status_does_not_invent_unknown_component_coverage(self):
        result, api = self.collect()
        self.assertEqual('INCOMPLETE', result['status'])
        self.assertEqual('PURL_MISSING', result['reasonCode'])
        self.assertEqual('PURL_COVERAGE_UNPROVEN', result['statusReconciliation']['coverage'])
        self.assertEqual(1, result['statusReconciliation']['unresolvedComponentCount'])
        self.assertEqual([{'groupId':'io.fireflysoftware', 'artifactId':'firefly-parent',
                           'version':'26.10.6', 'qualifiers':{'type':'pom'}}],
                         result['statusReconciliation']['deployments'][0]['statusComponents'])
        self.assertFalse(result['versionAllocated'])
        self.assertNotIn('PRIVATE-', json.dumps(result))
        self.assertNotIn('TOKEN-SENTINEL', json.dumps(result))
        self.assertEqual(4, len(api.requests))

    def test_only_exact_listed_id_empty_body_fixed_post_is_issued(self):
        _, api = self.collect()
        self.assertEqual(['GET', 'POST', 'GET', 'POST'], [r.get_method() for r, _ in api.requests])
        for request, timeout in api.requests[1::2]:
            self.assertEqual(STATUS_URL, request.full_url)
            self.assertEqual(b'', request.data)
            self.assertEqual('Bearer TOKEN-SENTINEL', request.get_header('Authorization'))
            self.assertEqual('identity', request.get_header('Accept-encoding'))
            self.assertLessEqual(timeout, 10)

    def test_status_identity_state_and_shape_are_validated(self):
        changes = [{'deploymentId':'different'}, {'deploymentState':'PUBLISHED'},
                   {'deploymentId':None}, {'deploymentState':'PENDING'},
                   {'purls':None}, {'purls':[]}, {'purls':'PRIVATE'}, {'purls':[None]}]
        for change in changes:
            value = status(); value.update(change)
            with self.subTest(change=change), self.assertRaises(m.InventoryError):
                self.collect(observation=value)

    def test_status_namespace_encoding_qualifiers_and_duplicates_are_not_relaxed(self):
        for purls in [['pkg:maven/evil/a@1'], [PURL, PURL],
                      ['pkg:maven/io.fireflysoftware/a@1', 'pkg:maven/io.fireflysoftware/a@1?type=jar'],
                      ['pkg:maven/io.fireflysoftware/a@1?unknown=PRIVATE'],
                      ['pkg:maven/io.fireflysoftware/a@1%252FPRIVATE']]:
            with self.subTest(purls=purls), self.assertRaises(m.InventoryError):
                self.collect(observation=status(purls))

    def test_mixed_known_listing_coordinate_must_be_present_in_status(self):
        row = unknown(); row['deploymentComponents'].append({'purl':'pkg:maven/io.fireflysoftware/a@1'})
        with self.assertRaises(m.InventoryError): self.collect([row])
        result, _ = self.collect([row], status([PURL, 'pkg:maven/io.fireflysoftware/a@1']))
        self.assertEqual('INCOMPLETE', result['status'])
        self.assertEqual(1, result['statusReconciliation']['unresolvedComponentCount'])

    def test_status_and_unresolved_count_must_match_across_sweeps(self):
        for second_status, second_row in [(status([PURL.replace('26.10.6','26.10.7')]), unknown()),
                                           (status(), deployment())]:
            api = Api([page([unknown()]), status(), page([second_row]), second_status])
            with self.subTest(second_status=second_status), self.assertRaises(m.InventoryError):
                m.collect('TOKEN', opener=api, page_size=2)

    def test_invalid_listing_is_rejected_before_status_lookup(self):
        for change in [{'namespace':'evil'}, {'deploymentId':'../evil'},
                       {'createTimestamp':None}, {'deploymentState':'PENDING'}]:
            row = unknown(); row.update(change); api = Api([page([row])])
            with self.subTest(change=change), self.assertRaises(m.InventoryError):
                m.collect('TOKEN', opener=api, page_size=2)
            self.assertEqual(['GET'], [r.get_method() for r, _ in api.requests])
        row = unknown(); row['deploymentComponents'].append({'purl':'pkg:maven/evil/a@1'})
        api = Api([page([row])])
        with self.assertRaises(m.InventoryError): m.collect('TOKEN', opener=api, page_size=2)
        self.assertEqual(1,len(api.requests))

    def test_known_inventory_never_makes_status_requests(self):
        api = Api([page(), page()])
        result = m.collect('TOKEN', opener=api, page_size=2)
        self.assertEqual('ACCOUNT_VISIBLE_NAMESPACE_STABLE_TWO_SWEEPS',result['status'])
        self.assertEqual(['GET','GET'],[r.get_method() for r,_ in api.requests])

    def test_status_transport_shares_url_content_json_and_byte_guards(self):
        replies = [Response(status(),'https://evil.example'),
                   Response(b'{"purls":[],"purls":[]}',STATUS_URL),
                   Response(b' '*(2*1024*1024+1),STATUS_URL),
                   Response(b'[]',STATUS_URL), Response(b'\xff',STATUS_URL)]
        for reply in replies:
            api = Api([page([unknown()]),reply])
            with self.subTest(reply=type(reply).__name__), self.assertRaises(m.InventoryError):
                m.collect('TOKEN',opener=api,page_size=2)
        reply = Response(status(),STATUS_URL); reply.headers={'Content-Type':'text/html'}
        with self.assertRaises(m.InventoryError):
            m.collect('TOKEN',opener=Api([page([unknown()]),reply]),page_size=2)

    def test_status_http_and_transport_errors_never_leak_private_values(self):
        for error in [OSError('PRIVATE-ERROR TOKEN-SENTINEL'),
                      urllib.error.HTTPError(STATUS_URL,403,'PRIVATE-ERROR',{},io.BytesIO(b'PRIVATE-BODY'))]:
            with self.assertRaises(m.InventoryError) as caught:
                m.collect('TOKEN',opener=Api([page([unknown()]),error]),page_size=2)
            self.assertNotIn('PRIVATE-',str(caught.exception))
            self.assertNotIn('TOKEN',str(caught.exception))

    def test_status_cardinality_is_bounded_and_input_not_mutated(self):
        value=status([PURL]*10001)
        with self.assertRaises(m.InventoryError): self.collect(observation=value)
        row=unknown(); before=copy.deepcopy(row)
        self.collect([row]); self.assertEqual(before,row)

    def test_status_observations_share_total_component_budget(self):
        with patch.object(m,'MAX_COMPONENTS',1), self.assertRaises(m.InventoryError):
            self.collect()

    def test_worker_returns_failure_for_stable_unproven_status_evidence(self):
        api=Api([page([unknown()]),status(),page([unknown()]),status()])
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'report.json'
            with patch.dict(m.os.environ,{'SONATYPE_USERNAME':'synthetic','SONATYPE_PASSWORD':'synthetic'},clear=True), \
                 patch.object(m.urllib.request,'build_opener',return_value=api):
                self.assertEqual(1,m.worker(output))
            result=json.loads(output.read_text())
            self.assertEqual('PURL_COVERAGE_UNPROVEN',result['statusReconciliation']['coverage'])
            self.assertEqual(0o600,output.stat().st_mode&0o777)

    def test_missing_listing_queries_status_before_closed_completeness_decision(self):
        api=Api([page([unknown()]),status(),page([unknown()]),status()])
        try:
            result=m.collect('TOKEN',opener=api,page_size=2)
        except m.InventoryError:
            result=None
        self.assertEqual(2,sum(request.get_method()=='POST' for request,_ in api.requests))
        self.assertEqual('INCOMPLETE',result['status'])
