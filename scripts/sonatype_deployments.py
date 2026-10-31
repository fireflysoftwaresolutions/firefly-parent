#!/usr/bin/env python3
"""Bounded, read-only account-visible Central Portal deployment reconciliation."""
import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = 'https://central.sonatype.com/api/v1/publisher/deployments'
NAMESPACE = 'io.fireflysoftware'
STATES = {'VALIDATING', 'VALIDATED', 'PUBLISHING', 'PUBLISHED', 'FAILED'}
MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 100
MAX_COMPONENTS = 100000
WORK_SECONDS = 120
PURL = re.compile(r'pkg:maven/io\.fireflysoftware/([A-Za-z0-9][A-Za-z0-9_.-]{0,199})@([A-Za-z0-9][A-Za-z0-9_.+-]{0,127})')
QUALIFIER_VALUE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')


class InventoryError(ValueError):
    """Only fixed, credential-free diagnostics may cross this boundary."""
    def __init__(self, message, reason_code=None):
        super().__init__(message)
        self.reason_code = reason_code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InventoryError('Portal redirects are refused.')


def token(username, password):
    if (not username or not password or ':' in username
            or len(username) > 4096 or len(password) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in username + password)):
        raise InventoryError('Portal token pair is missing or invalid.')
    return base64.b64encode((username + ':' + password).encode()).decode('ascii')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError('Portal JSON has duplicate object keys.')
        result[key] = value
    return result


def fetch(opener, bearer, page, size):
    query = urllib.parse.urlencode({'namespace': NAMESPACE, 'page': page, 'size': size,
                                   'sortField': 'createTimestamp', 'sortDirection': 'asc'})
    url = ENDPOINT + '?' + query
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + bearer,
                                     'Accept': 'application/json', 'Accept-Encoding': 'identity'}, method='GET')
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200 or response.geturl() != url:
                raise InventoryError('Portal response status or URL is unsupported.')
            if response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                raise InventoryError('Portal response must be JSON.')
            payload = response.read(MAX_BYTES + 1)
    except InventoryError:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        raise InventoryError('Portal HTTP request was refused.') from None
    except (OSError, ValueError):
        raise InventoryError('Portal transport failed.') from None
    if len(payload) > MAX_BYTES:
        raise InventoryError('Portal response exceeded the byte limit.')
    try:
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=unique_object,
                           parse_constant=lambda _: reject_json_constant())
    except (UnicodeError, ValueError, RecursionError):
        raise InventoryError('Portal JSON is invalid or unsupported.') from None
    if not isinstance(value, dict):
        raise InventoryError('Portal response must be an object.')
    return value


def reject_json_constant():
    raise InventoryError('Portal JSON constants are unsupported.')


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40 or not value.endswith('Z'):
        raise InventoryError('Portal deployment timestamp is unsupported.')
    try:
        datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        raise InventoryError('Portal deployment timestamp is unsupported.') from None
    return value



def coordinate_error(code):
    raise InventoryError('Portal component Maven coordinates are unsupported.', code) from None


def decode_coordinate(value):
    if re.search(r'%(?![0-9A-Fa-f]{2})', value):
        coordinate_error('PURL_ENCODING')
    try:
        decoded = urllib.parse.unquote_to_bytes(value).decode('utf-8')
    except UnicodeError:
        coordinate_error('PURL_ENCODING')
    if any(ord(c) <= 32 or ord(c) == 127 or c in '/@?#&=:%' for c in decoded):
        coordinate_error('PURL_ENCODING')
    return decoded


def maven_coordinate(component):
    if not isinstance(component, dict):
        coordinate_error('PURL_COMPONENT')
    purl = component.get('purl')
    if not isinstance(purl, str) or not purl:
        coordinate_error('PURL_MISSING')
    if len(purl) > 2048:
        coordinate_error('PURL_BOUNDS')
    scheme, colon, rest = purl.partition(':')
    if colon != ':' or scheme != 'pkg':
        coordinate_error('PURL_SCHEME')
    package_type, slash, path = rest.partition('/')
    if slash != '/' or package_type != 'maven':
        coordinate_error('PURL_TYPE')
    if '#' in path:
        coordinate_error('PURL_SUBPATH')
    base, question, query = path.partition('?')
    namespace, slash, artifact_version = base.partition('/')
    if slash != '/' or '/' in artifact_version:
        coordinate_error('PURL_COORDINATES')
    namespace = decode_coordinate(namespace)
    if namespace != NAMESPACE:
        coordinate_error('PURL_NAMESPACE')
    artifact, at, version = artifact_version.rpartition('@')
    if at != '@':
        coordinate_error('PURL_COORDINATES')
    artifact, version = decode_coordinate(artifact), decode_coordinate(version)
    if not PURL.fullmatch('pkg:maven/' + namespace + '/' + artifact + '@' + version):
        coordinate_error('PURL_COORDINATES')
    qualifiers = {}
    if question:
        pairs = query.split('&')
        if not query or '?' in query or len(pairs) > 2:
            coordinate_error('PURL_QUALIFIER')
        for pair in pairs:
            key, equal, value = pair.partition('=')
            if '%' in key:
                coordinate_error('PURL_ENCODING')
            key = key.lower()
            if equal != '=' or key not in ('type', 'classifier'):
                coordinate_error('PURL_QUALIFIER')
            if key in qualifiers:
                coordinate_error('PURL_QUALIFIER_DUPLICATE')
            value = decode_coordinate(value)
            if not QUALIFIER_VALUE.fullmatch(value):
                coordinate_error('PURL_QUALIFIER')
            qualifiers[key] = value
    # Maven defaults to jar; other type/classifier values remain part of stable identity.
    if qualifiers.get('type') == 'jar':
        del qualifiers['type']
    return artifact, version, tuple(sorted(qualifiers.items()))


def project(row):
    if not isinstance(row, dict):
        raise InventoryError('Portal deployment is unsupported.')
    identifier = row.get('deploymentId')
    state = row.get('deploymentState')
    components = row.get('deploymentComponents')
    if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier)
            or row.get('namespace') != NAMESPACE or not isinstance(state, str) or state not in STATES):
        raise InventoryError('Portal deployment identity, namespace or state is unsupported.')
    if not isinstance(components, list) or not components or len(components) > 10000:
        raise InventoryError('Portal deployment components are missing or unsupported.')
    coordinates = set()
    for component in components:
        coordinates.add(maven_coordinate(component))
    return {'deploymentId': identifier, 'state': state,
            'createTimestamp': timestamp(row.get('createTimestamp')),
            'updateTimestamp': timestamp(row.get('updateTimestamp')),
            'components': [{'groupId': NAMESPACE, 'artifactId': artifact, 'version': version,
                            **({'qualifiers': dict(qualifiers)} if qualifiers else {})}
                           for artifact, version, qualifiers in sorted(coordinates)]}


def sweep(opener, bearer, page_size):
    rows = []
    identifiers = set()
    metadata = None
    total_components = 0
    number = 0
    while True:
        reply = fetch(opener, bearer, number, page_size)
        keys = ('page', 'pageSize', 'pageCount', 'totalResultCount')
        if any(type(reply.get(key)) is not int or reply[key] < 0 for key in keys):
            raise InventoryError('Portal pagination metadata is missing or unsupported.')
        size, count, total = (reply[key] for key in keys[1:])
        if (reply['page'] != number or not 1 <= size <= page_size
                or count > MAX_PAGES or total > MAX_PAGES * size
                or (total > 0 and count != (total + size - 1) // size)
                or (total == 0 and count not in (0, 1))):
            raise InventoryError('Portal pagination bounds or cardinality are inconsistent.')
        current = (size, count, total)
        if metadata is not None and current != metadata:
            raise InventoryError('Portal inventory changed during pagination.')
        metadata = current
        deployments = reply.get('deployments')
        if not isinstance(deployments, list) or len(deployments) != min(size, total - number * size):
            raise InventoryError('Portal page is incomplete.')
        for row in deployments:
            safe = project(row)
            if safe['deploymentId'] in identifiers:
                raise InventoryError('Portal deployment was repeated across pages.')
            identifiers.add(safe['deploymentId'])
            total_components += len(row['deploymentComponents'])
            if total_components > MAX_COMPONENTS:
                raise InventoryError('Portal inventory exceeded the component limit.')
            rows.append(safe)
        number += 1
        if number >= count:
            break
    if len(rows) != metadata[2]:
        raise InventoryError('Portal inventory cardinality is incomplete.')
    return metadata, sorted(rows, key=lambda row: row['deploymentId'])


def collect(bearer, opener=None, page_size=100):
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise InventoryError('Portal page size is unsupported.')
    if opener is None:
        # Never forward credentials through environment-selected proxies or redirects.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    first = sweep(opener, bearer, page_size)
    second = sweep(opener, bearer, page_size)
    if first != second:
        raise InventoryError('Portal inventory changed between complete sweeps.')
    metadata, rows = first
    return {'schema': 1, 'status': 'ACCOUNT_VISIBLE_NAMESPACE_STABLE_TWO_SWEEPS',
            'namespace': NAMESPACE, 'endpoint': ENDPOINT, 'atomicSnapshot': False,
            'completenessScope': 'Documented account-visible namespace listing; two identical complete sweeps only.',
            'pageSize': metadata[0], 'pageCount': metadata[1], 'deploymentCount': len(rows),
            'deployments': rows, 'pendingDeployments': [row for row in rows if row['state'] != 'PUBLISHED'],
            'versionAllocated': False, 'releasePublished': False}


def write_report(output, report):
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')


def incomplete(reason='Portal reconciliation did not complete.', reason_code=None):
    return {'schema': 1, 'status': 'INCOMPLETE', 'namespace': NAMESPACE, 'reason': reason,
            **({'reasonCode': reason_code} if reason_code is not None else {}),
            'versionAllocated': False, 'releasePublished': False}


def worker(output):
    try:
        bearer = token(os.environ.get('SONATYPE_USERNAME', ''), os.environ.get('SONATYPE_PASSWORD', ''))
        report = collect(bearer)
        write_report(output, report)
        return 0
    except InventoryError as error:
        write_report(output, incomplete(str(error), error.reason_code))
        return 1
    except Exception:
        # Third-party exceptions can include URLs, headers or response bodies.
        write_report(output, incomplete())
        return 1


def run_bounded(output):
    write_report(output, incomplete())
    try:
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                 '--internal-worker', '--output', str(output)],
                                timeout=WORK_SECONDS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False)
        code = result.returncode
    except (subprocess.TimeoutExpired, OSError):
        write_report(output, incomplete())
        return 1
    if code != 0:
        if code != 1:
            write_report(output, incomplete())
        else:
            try:
                if json.loads(Path(output).read_text()).get('status') != 'INCOMPLETE':
                    write_report(output, incomplete())
            except (OSError, ValueError, AttributeError):
                write_report(output, incomplete())
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--internal-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.internal_worker:
        return worker(args.output)
    code = run_bounded(args.output)
    print('Portal read-only inventory complete.' if code == 0 else 'Portal read-only inventory incomplete; allocation remains blocked.')
    return code


if __name__ == '__main__':
    sys.exit(main())
