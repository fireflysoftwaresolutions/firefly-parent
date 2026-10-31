# Account-visible deployment-state census

This is a separate GET-only observation. The existing coordinate inventory and its
INCOMPLETE results retain their original meaning. A state census never establishes
complete GAVs, reservation history or version allocation.

The default-branch-only manual `Read-only Sonatype deployment-state census`
workflow queries only the fixed authenticated GET
`https://central.sonatype.com/api/v1/publisher/deployments` for exact namespace
`io.fireflysoftware`, without a state filter. It reuses the reviewed transport,
in-memory credential encoding, no-proxy/no-redirect behavior and strict bounded
JSON parsing. It never calls POST status, upload, publish, delete, Maven or signing.
Names, paths, PURLs, errors and other component values do not supply coordinates
and do not appear in the report.

Every row must have an exact supported deployment ID, namespace, GET-supported
state and timestamps. The component array must have known bounded cardinality;
its items are objects, but their coordinate fields may be missing, null or
unsupported. The state census accepts empty component arrays because it does not
claim coordinate coverage. Missing/malformed arrays still refuse. Pagination is
complete and cardinality-consistent with unique deployment IDs. Both full sweeps
must match IDs, states, timestamps and component counts. Original limits remain:
120-second outer worker,2MiB per response,100 pages and100000 components per sweep,
up to100 rows per page and10000 component entries per deployment. No retry or
separate per-phase budget is added.

A complete report has `ACCOUNT_VISIBLE_STATE_CENSUS_STABLE_TWO_SWEEPS`, scope
`GET_ACCOUNT_VISIBLE_DEPLOYMENT_ID_STATE_ONLY`, explicit pagination and counts,
`coordinateInventoryAccepted=false`, `versionAllocated=false` and
`releasePublished=false`. `noPendingDeploymentsGatePassed` is true only if every
observed deployment is PUBLISHED; authenticated empty is explicit zero. FAILED,
VALIDATING, VALIDATED and PUBLISHING remain in `pendingDeployments` and block that
separate gate. Unsupported states, partial/unstable pagination, transport or shape
failures produce INCOMPLETE. Pending but complete state evidence is retained with
worker/CLI exit1; it is never relabeled an ordinary success. No raw body or private
display text crosses the report/log boundary.

The official [GET listing](https://central.sonatype.com/api-doc) is account-visible,
not a global or historical registry. [Deployment retention](https://central.sonatype.org/faq/what-happened-to-my-deployments/)
can remove old records. Two equal sweeps are not an atomic cross-registry snapshot
or a lock against concurrent publishers. Root must separately review the actual
future workflow/source/artifact/account observation, exact candidate Central
coordinates/current version metadata, GitHub counters/tags, private source/staging/
reservation history and concurrency controls before any allocation or publication.
This source creates no runtime authority and chooses/reserves no monthly counter.
Parent/Jafly dependency pins and the existing coordinate API/workflow stay exact.
