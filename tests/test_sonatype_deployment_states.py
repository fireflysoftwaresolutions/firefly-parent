"""A deployment-state census never claims coordinates or allocation authority."""
import copy
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
import urllib.error
from test_sonatype_deployments import Api, Response, deployment, page

SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE/'scripts/sonatype_deployment_states.py'
sys.path.insert(0,str(SOURCE/'scripts'))
m = None
if SCRIPT.exists():
    spec=importlib.util.spec_from_file_location('state_inventory',SCRIPT)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def row(number=1,state='PUBLISHED'):
    value=deployment(number,state)
    value['deploymentComponents'][0].update(purl=None,warnings=None)
    return value


class DeploymentStateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(m,'Separate GET-only deployment-state census is absent')

    def collect(self,rows=None):
        rows=[row()] if rows is None else rows
        api=Api([page(rows),page(copy.deepcopy(rows))])
        return m.collect('TOKEN-SENTINEL',opener=api,page_size=2),api

    def test_published_null_purl_census_is_complete_only_for_states(self):
        result,api=self.collect()
        self.assertEqual('ACCOUNT_VISIBLE_STATE_CENSUS_STABLE_TWO_SWEEPS',result['status'])
        self.assertEqual('GET_ACCOUNT_VISIBLE_DEPLOYMENT_ID_STATE_ONLY',result['inventoryScope'])
        self.assertFalse(result['coordinateInventoryAccepted']);self.assertFalse(result['versionAllocated'])
        self.assertFalse(result['releasePublished']);self.assertFalse(result['atomicSnapshot'])
        self.assertTrue(result['noPendingDeploymentsGatePassed'])
        self.assertEqual((2,1,1,1),(result['pageSize'],result['pageCount'],result['deploymentCount'],result['componentCount']))
        self.assertEqual(2,result['matchingSweeps'])
        self.assertEqual([{'deploymentId':'deployment-1','state':'PUBLISHED','componentCount':1,
                           'createTimestamp':'2026-10-31T10:00:00.000Z','updateTimestamp':'2026-10-31T10:01:00.000Z'}],result['deployments'])
        self.assertNotIn('PRIVATE-',json.dumps(result));self.assertNotIn('TOKEN-SENTINEL',json.dumps(result))
        self.assertEqual([],result['pendingDeployments'])
        for request,timeout in api.requests:
            self.assertEqual('GET',request.get_method());self.assertIsNone(request.data)
            self.assertEqual('https://central.sonatype.com/api/v1/publisher/deployments?namespace=io.fireflysoftware&page=0&size=2&sortField=createTimestamp&sortDirection=asc',request.full_url)
            self.assertEqual('Bearer TOKEN-SENTINEL',request.get_header('Authorization'));self.assertLessEqual(timeout,10)

    def test_each_nonpublished_supported_state_is_retained_and_blocks_no_pending(self):
        for state in ['VALIDATING','VALIDATED','PUBLISHING','FAILED']:
            with self.subTest(state=state):
                result,_=self.collect([row(1,'PUBLISHED'),row(2,state)])
                self.assertEqual('ACCOUNT_VISIBLE_STATE_CENSUS_STABLE_TWO_SWEEPS',result['status'])
                self.assertFalse(result['noPendingDeploymentsGatePassed'])
                self.assertEqual([state],[v['state']for v in result['pendingDeployments']])
                self.assertEqual(2,result['deploymentCount'])

    def test_all_pages_are_counted_before_two_sweep_acceptance(self):
        pages=[page([row(1),row(2,'FAILED')],total=3),page([row(3)],number=1,total=3)]
        api=Api(pages+copy.deepcopy(pages));result=m.collect('TOKEN',opener=api,page_size=2)
        self.assertEqual(3,result['deploymentCount']);self.assertEqual(2,result['pageCount'])
        self.assertEqual(['deployment-1','deployment-2','deployment-3'],[v['deploymentId']for v in result['deployments']])
        self.assertFalse(result['noPendingDeploymentsGatePassed']);self.assertEqual(4,len(api.requests))

    def test_authenticated_empty_is_explicit_zero_not_unknown(self):
        for pages in [0,1]:
            value=page([]);value['pageCount']=pages
            result=m.collect('TOKEN',opener=Api([value,copy.deepcopy(value)]),page_size=2)
            self.assertEqual((0,0),(result['deploymentCount'],result['componentCount']))
            self.assertTrue(result['noPendingDeploymentsGatePassed']);self.assertFalse(result['coordinateInventoryAccepted'])

    def test_identity_namespace_state_and_timestamps_are_not_relaxed(self):
        changes=[{'deploymentId':'../PRIVATE'},{'deploymentId':None},{'namespace':'other'},
                 {'deploymentState':'PENDING'},{'deploymentState':None},{'deploymentState':'DELETED'},
                 {'createTimestamp':None},{'updateTimestamp':'PRIVATE'},{'deploymentState':True}]
        for change in changes:
            value=row();value.update(change)
            with self.subTest(change=change),self.assertRaises(m.portal.InventoryError):self.collect([value])

    def test_component_cardinality_is_known_even_when_coordinate_values_are_unknown(self):
        for components in [None,{},'PRIVATE',[None],[1]]:
            value=row();value['deploymentComponents']=components
            with self.subTest(kind=type(components).__name__),self.assertRaises(m.portal.InventoryError):self.collect([value])
        value=row();value['deploymentComponents']=[]
        result,_=self.collect([value]);self.assertEqual(0,result['componentCount'])
        value['deploymentComponents']=[{}]*10001
        with self.assertRaises(m.portal.InventoryError):self.collect([value])

    def test_coordinate_display_values_never_determine_the_state_result_or_trigger_post(self):
        first=row();second=copy.deepcopy(first)
        second['deploymentComponents'][0].update(purl='PRIVATE-NOT-A-PURL',name='PRIVATE-OTHER',path='PRIVATE-OTHER',errors=['PRIVATE'])
        api=Api([page([first]),page([second])])
        with patch.object(m.portal,'fetch_status',side_effect=AssertionError('POST is forbidden')):
            result=m.collect('TOKEN',opener=api,page_size=2)
        self.assertTrue(result['noPendingDeploymentsGatePassed']);self.assertFalse(result['coordinateInventoryAccepted'])
        self.assertEqual(['GET','GET'],[r.get_method()for r,_ in api.requests])

    def test_two_sweep_id_state_timestamp_and_count_drift_refuse(self):
        for change in [{'deploymentId':'deployment-2'},{'deploymentState':'FAILED'},
                       {'updateTimestamp':'2026-10-31T10:02:00Z'},{'deploymentComponents':[{},{}]}]:
            second=row();second.update(change)
            with self.subTest(change=change),self.assertRaises(m.portal.InventoryError):
                m.collect('TOKEN',opener=Api([page([row()]),page([second])]),page_size=2)

    def test_pagination_types_counts_shift_and_duplicate_ids_refuse(self):
        for key in ['page','pageSize','pageCount','totalResultCount']:
            value=page([row()]);value[key]=True
            with self.subTest(key=key),self.assertRaises(m.portal.InventoryError):m.collect('TOKEN',opener=Api([value]),page_size=2)
        for value in [page([row(),row()],total=2),page([row()],total=2)]:
            with self.assertRaises(m.portal.InventoryError):m.collect('TOKEN',opener=Api([value]),page_size=2)
        pages=[page([row(1),row(2)],total=3),page([row(3)],number=1,total=4)]
        with self.assertRaises(m.portal.InventoryError):m.collect('TOKEN',opener=Api(pages),page_size=2)

    def test_original_page_and_component_caps_are_shared(self):
        value=page([row()]);value.update(pageCount=101,totalResultCount=202)
        with self.assertRaises(m.portal.InventoryError):m.collect('TOKEN',opener=Api([value]),page_size=2)
        with patch.object(m.portal,'MAX_COMPONENTS',1),self.assertRaises(m.portal.InventoryError):self.collect([row(1),row(2)])
        for size in [0,101,True,None]:
            with self.subTest(size=size),self.assertRaises(m.portal.InventoryError):m.collect('TOKEN',opener=Api([]),page_size=size)

    def test_shared_transport_errors_are_closed_not_empty_and_never_expose_body(self):
        url='https://central.sonatype.com/api/v1/publisher/deployments?namespace=io.fireflysoftware&page=0&size=2&sortField=createTimestamp&sortDirection=asc'
        for reply in [Response(b'{"page":0,"page":1}',url),Response(b'[]',url),Response(b'\xff',url),
                      Response(b' '*(2*1024*1024+1),url),Response(page([row()]),'https://evil.invalid'),
                      OSError('PRIVATE TOKEN-SENTINEL'),urllib.error.HTTPError(url,404,'PRIVATE',{},io.BytesIO(b'PRIVATE-BODY'))]:
            with self.assertRaises(m.portal.InventoryError)as caught:m.collect('TOKEN',opener=Api([reply]),page_size=2)
            self.assertNotIn('PRIVATE',str(caught.exception));self.assertNotIn('TOKEN',str(caught.exception))

    def test_default_opener_explicitly_disables_environment_proxies_and_redirects(self):
        api=Api([page([row()]),page([row()])])
        with patch.object(m.portal.urllib.request,'build_opener',return_value=api)as build:
            self.assertTrue(m.collect('TOKEN',page_size=2)['noPendingDeploymentsGatePassed'])
        handlers=build.call_args.args
        self.assertEqual({},handlers[0].proxies);self.assertIsInstance(handlers[1],m.portal.NoRedirect)
        self.assertEqual(['GET','GET'],[r.get_method()for r,_ in api.requests])

    def test_worker_pending_is_complete_state_evidence_but_exit_one(self):
        api=Api([page([row(state='FAILED')]),page([row(state='FAILED')])])
        with tempfile.TemporaryDirectory()as directory:
            out=Path(directory)/'state.json'
            with patch.dict(os.environ,{'SONATYPE_USERNAME':'synthetic','SONATYPE_PASSWORD':'synthetic'},clear=True),patch.object(m.portal.urllib.request,'build_opener',return_value=api):
                self.assertEqual(1,m.worker(out))
            result=json.loads(out.read_text());self.assertEqual('ACCOUNT_VISIBLE_STATE_CENSUS_STABLE_TWO_SWEEPS',result['status'])
            self.assertFalse(result['noPendingDeploymentsGatePassed']);self.assertEqual(0o600,out.stat().st_mode&0o777)
            with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],1)):
                # The parent reinitializes before its worker; model the child publishing the same report.
                def child(*args,**kwargs):m.portal.write_report(out,result);return subprocess.CompletedProcess([],1)
                with patch.object(m.subprocess,'run',side_effect=child):self.assertEqual(1,m.run_bounded(out))
            self.assertEqual(result,json.loads(out.read_text()))

    def test_worker_all_published_closes_only_the_separate_no_pending_gate(self):
        api=Api([page([row()]),page([row()])])
        with tempfile.TemporaryDirectory()as directory:
            out=Path(directory)/'state.json'
            with patch.dict(os.environ,{'SONATYPE_USERNAME':'synthetic','SONATYPE_PASSWORD':'synthetic'},clear=True),patch.object(m.portal.urllib.request,'build_opener',return_value=api):
                self.assertEqual(0,m.worker(out))
            result=json.loads(out.read_text());self.assertTrue(result['noPendingDeploymentsGatePassed']);self.assertFalse(result['versionAllocated'])

    def test_timeout_or_abnormal_child_cannot_leave_late_success(self):
        with tempfile.TemporaryDirectory()as directory:
            out=Path(directory)/'state.json'
            def overrun(*args,**kwargs):
                result,_=self.collect();m.portal.write_report(out,result)
                raise subprocess.TimeoutExpired('PRIVATE',120)
            with patch.object(m.subprocess,'run',side_effect=overrun)as run:self.assertEqual(1,m.run_bounded(out))
            self.assertEqual(120,run.call_args.kwargs['timeout']);self.assertEqual('INCOMPLETE',json.loads(out.read_text())['status'])
            self.assertFalse(json.loads(out.read_text())['coordinateInventoryAccepted'])
            with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],7)):self.assertEqual(1,m.run_bounded(out))
            self.assertEqual('INCOMPLETE',json.loads(out.read_text())['status'])

    def test_original_coordinate_inventory_still_refuses_empty_status_purls(self):
        failed=row();reply={'deploymentId':failed['deploymentId'],'deploymentState':'PUBLISHED','purls':[]}
        with self.assertRaises(m.portal.InventoryError):m.portal.collect('TOKEN',opener=Api([page([failed]),reply]),page_size=2)
        states,_=self.collect([failed]);self.assertTrue(states['noPendingDeploymentsGatePassed'])
        self.assertFalse(states['coordinateInventoryAccepted'])

    def test_cli_missing_credentials_never_uses_network_or_exposes_secrets(self):
        with tempfile.TemporaryDirectory()as directory:
            out=Path(directory)/'state.json';result=subprocess.run([sys.executable,'-B',str(SCRIPT),'--output',str(out)],env={'PATH':os.environ['PATH']},capture_output=True,text=True,timeout=10)
            self.assertEqual(1,result.returncode);self.assertNotIn('Traceback',result.stderr)
            report=json.loads(out.read_text());self.assertEqual('INCOMPLETE',report['status']);self.assertFalse(report['coordinateInventoryAccepted']);self.assertFalse(report['versionAllocated'])

    def test_new_manual_workflow_is_get_state_only_without_publication_tools(self):
        path=SOURCE/'.github/workflows/sonatype-state-preflight.yml'
        workflow=json.loads(subprocess.check_output(['ruby','-rjson','-ryaml','-e','puts JSON.generate(YAML.safe_load(File.read(ARGV[0])))',str(path)],text=True))
        self.assertEqual({'workflow_dispatch'},set(workflow['true']));self.assertEqual({'contents':'read'},workflow['permissions'])
        job,=workflow['jobs'].values();self.assertEqual(5,job['timeout-minutes'])
        self.assertEqual("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",job['if'])
        checkout,inventory,upload=job['steps']
        self.assertEqual('actions/checkout@11d5960a326750d5838078e36cf38b85af677262',checkout['uses'])
        self.assertEqual({'ref':'${{ github.sha }}','persist-credentials':False},checkout['with'])
        self.assertEqual('python3 -B scripts/sonatype_deployment_states.py --output sonatype-deployment-states.json',inventory['run'])
        self.assertEqual({'SONATYPE_USERNAME':'${{ secrets.SONATYPE_USERNAME }}','SONATYPE_PASSWORD':'${{ secrets.SONATYPE_PASSWORD }}'},inventory['env'])
        self.assertEqual('actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',upload['uses'])
        self.assertEqual('always()',upload['if']);self.assertEqual('sonatype-deployment-states.json',upload['with']['path'])
        self.assertNotIn('env',job);self.assertNotIn('secrets',job)

    def test_timestamps_require_the_documented_full_date_time_shape(self):
        for value in ['2026-10-31Z','20261031T100000Z','2026-10-31T10:00Z',
                      '2026-10-31 10:00:00Z','2026-W44-6T10:00:00Z']:
            item=row();item['createTimestamp']=value
            with self.subTest(value=value),self.assertRaises(m.portal.InventoryError):self.collect([item])
        item=row();item['updateTimestamp']='2026-10-31T10:01:00.123456789Z'
        result,_=self.collect([item])
        self.assertEqual('2026-10-31T10:01:00.123456789Z',result['deployments'][0]['updateTimestamp'])
