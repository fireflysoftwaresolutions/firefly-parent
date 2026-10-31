#!/usr/bin/env python3
"""Bounded GET-only account-visible deployment states; never coordinate or version admission."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sonatype_deployments as portal

STATUS = 'ACCOUNT_VISIBLE_STATE_CENSUS_STABLE_TWO_SWEEPS'
SCOPE = 'GET_ACCOUNT_VISIBLE_DEPLOYMENT_ID_STATE_ONLY'


def timestamp(value):
    if (not isinstance(value, str)
            or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,19})?Z', value)):
        raise portal.InventoryError('Portal state timestamp must be a supported full date-time.')
    return portal.timestamp(value)


def project(row):
    if not isinstance(row, dict):
        raise portal.InventoryError('Portal deployment state row is unsupported.')
    identifier = row.get('deploymentId')
    state = row.get('deploymentState')
    if (not isinstance(identifier, str) or not portal.IDENTIFIER.fullmatch(identifier)
            or row.get('namespace') != portal.NAMESPACE or not isinstance(state, str) or state not in portal.STATES):
        raise portal.InventoryError('Portal deployment identity, namespace or state is unsupported.')
    components = row.get('deploymentComponents')
    if (not isinstance(components, list) or len(components) > 10000
            or any(not isinstance(component, dict) for component in components)):
        raise portal.InventoryError('Portal component cardinality is unsupported.')
    return {'deploymentId': identifier, 'state': state, 'componentCount': len(components),
            'createTimestamp': timestamp(row.get('createTimestamp')),
            'updateTimestamp': timestamp(row.get('updateTimestamp'))}


def sweep(opener, bearer, page_size):
    rows = []
    identifiers = set()
    metadata = None
    components = 0
    number = 0
    while True:
        reply = portal.fetch(opener, bearer, number, page_size)
        keys = ('page', 'pageSize', 'pageCount', 'totalResultCount')
        if any(type(reply.get(key)) is not int or reply[key] < 0 for key in keys):
            raise portal.InventoryError('Portal state pagination metadata is unsupported.')
        size, count, total = (reply[key] for key in keys[1:])
        if (reply['page'] != number or not 1 <= size <= page_size
                or count > portal.MAX_PAGES or total > portal.MAX_PAGES * size
                or (total > 0 and count != (total + size - 1) // size)
                or (total == 0 and count not in (0, 1))):
            raise portal.InventoryError('Portal state pagination bounds or cardinality are inconsistent.')
        current = (size, count, total)
        if metadata is not None and current != metadata:
            raise portal.InventoryError('Portal state inventory changed during pagination.')
        metadata = current
        deployments = reply.get('deployments')
        if not isinstance(deployments, list) or len(deployments) != min(size, total - number * size):
            raise portal.InventoryError('Portal state page is incomplete.')
        for row in deployments:
            safe = project(row)
            if safe['deploymentId'] in identifiers:
                raise portal.InventoryError('Portal state deployment was repeated across pages.')
            identifiers.add(safe['deploymentId'])
            components += safe['componentCount']
            if components > portal.MAX_COMPONENTS:
                raise portal.InventoryError('Portal state inventory exceeded the component limit.')
            rows.append(safe)
        number += 1
        if number >= count:
            break
    if len(rows) != metadata[2]:
        raise portal.InventoryError('Portal state inventory cardinality is incomplete.')
    return metadata, components, sorted(rows, key=lambda row: row['deploymentId'])


def collect(bearer, opener=None, page_size=100):
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise portal.InventoryError('Portal state page size is unsupported.')
    if opener is None:
        opener = portal.urllib.request.build_opener(portal.urllib.request.ProxyHandler({}), portal.NoRedirect())
    first = sweep(opener, bearer, page_size)
    second = sweep(opener, bearer, page_size)
    if first != second:
        raise portal.InventoryError('Portal state inventory changed between complete sweeps.')
    metadata, components, rows = first
    pending = [row for row in rows if row['state'] != 'PUBLISHED']
    return {'schema': 1, 'status': STATUS, 'inventoryScope': SCOPE,
            'namespace': portal.NAMESPACE, 'endpoint': portal.ENDPOINT, 'httpMethod': 'GET',
            'coordinateInventoryAccepted': False, 'versionAllocated': False, 'releasePublished': False,
            'atomicSnapshot': False, 'matchingSweeps': 2, 'pageSize': metadata[0],
            'pageCount': metadata[1], 'deploymentCount': metadata[2], 'componentCount': components,
            'deployments': rows, 'pendingDeployments': pending, 'noPendingDeploymentsGatePassed': not pending,
            'completenessScope': 'Only authenticated account-visible exact-namespace deployment IDs and states; no coordinate, global or historical coverage.'}


def incomplete(reason='Portal state census did not complete.'):
    return {**portal.incomplete(reason), 'inventoryScope': SCOPE, 'coordinateInventoryAccepted': False,
            'noPendingDeploymentsGatePassed': False}


def worker(output):
    try:
        bearer = portal.token(os.environ.get('SONATYPE_USERNAME', ''), os.environ.get('SONATYPE_PASSWORD', ''))
        report = collect(bearer)
        portal.write_report(output, report)
        return 0 if report['noPendingDeploymentsGatePassed'] else 1
    except portal.InventoryError as error:
        portal.write_report(output, incomplete(str(error)))
        return 1
    except Exception:
        portal.write_report(output, incomplete())
        return 1


def run_bounded(output):
    portal.write_report(output, incomplete())
    try:
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                 '--internal-worker', '--output', str(output)],
                                timeout=portal.WORK_SECONDS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False)
        if result.returncode not in (0, 1):
            raise ValueError('Worker did not complete.')
        report = json.loads(Path(output).read_text())
        if (report.get('inventoryScope') != SCOPE or report.get('coordinateInventoryAccepted') is not False
                or report.get('versionAllocated') is not False or report.get('releasePublished') is not False):
            raise ValueError('State-only report required.')
        if report.get('status') == 'INCOMPLETE' and result.returncode == 1:
            return 1
        if report.get('status') != STATUS or type(report.get('noPendingDeploymentsGatePassed')) is not bool:
            raise ValueError('Complete state census or closed failure required.')
        if result.returncode != (0 if report['noPendingDeploymentsGatePassed'] else 1):
            raise ValueError('Worker gate result is inconsistent.')
        return result.returncode
    except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError):
        portal.write_report(output, incomplete())
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--internal-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.internal_worker:
        return worker(args.output)
    code = run_bounded(args.output)
    print('Account-visible state census passed its no-pending gate; coordinate and allocation gates remain separate.'
          if code == 0 else 'State census or no-pending gate did not pass; allocation remains blocked.')
    return code


if __name__ == '__main__':
    sys.exit(main())
