# Central release prerequisites

The release policy explicitly selects prior publisher primary fingerprint
`A89AED672B71B245F3E1C6260933FD1E179EF8A1`. This is the primary key, not a
new key allocation or the observed signing subkey
`4B5FD4943C0984BD0CDFA01E97CE71BA687EEFA5`.

The public-only signing-anchor review verified detached signatures on both
published artifacts with `gpgv` and an explicit inspected public keyring:

| Artifact | Published POM SHA-256 |
| --- | --- |
| `io.fireflysoftware:firefly-parent:26.10.6` | `4220362f2d8de42dd1bebfa73799e05e390b46cb7dc439cb16f0d2a556361af0` |
| `org.fireflyframework:fireflyframework-parent:26.10.20` | `8f08db34ee054765b3f36cd6d443e446e30348fe0215ca65785be318191ea956` |

The prior signing subkey verified against that primary fingerprint. The
[public key source](https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xA89AED672B71B245F3E1C6260933FD1E179EF8A1)
had SHA-256 `9e4ed265f93b5208a3e555caf0b169494ece9dba66acfc99dca3edf94d4b373b`.
This proves prior public signatures. It does not prove access to the current
private key, a successful hosted signing operation, or current publishing rights.

The manual `Read-only Sonatype namespace inventory` workflow runs only from the
repository default branch. It uses the exact workflow source, only the Sonatype
credential pair, authenticated GET listing requests and, only for missing or null
component PURLs, an empty-body POST to the documented read-only
`/api/v1/publisher/status?id=<listed-deployment-id>` query. Both endpoints use
fixed HTTPS paths and the fixed `io.fireflysoftware` namespace. Credentials never enter the report. Redirects
and environment proxies are refused. Two bounded complete sweeps must agree;
failures produce an explicit incomplete report. Reports retain supported Maven
type/classifier identity and fixed safe reason codes. The worker limit is 120 seconds,
each response is capped at 2 MiB, and the listing is capped at 100 pages/100000 components.

The parser originated in the independently qualified Jafly source with its
two namespace constants adapted. Original source SHA-256:
`9a2582372c56486145fbdc74a88c76b42c913fb86d24aa430d77c1ad87889c67`.
An authenticated empty listing is distinct from a404 or incomplete listing.
Account-visible two-sweep stability is not an atomic or global Portal snapshot.
The workflow does not upload, publish, allocate a version, sign, or run Maven.

An unsupported component with a missing, empty or non-string PURL still makes the
inventory incomplete. Its failure report includes only a bounded structural
description: fixed known-field type labels, field counts, and at most eight
SHA-256 hashes of unknown field names. Component values, raw bodies, names, paths,
errors and credentials are excluded. This diagnostic does not supply coordinates
or permit version allocation; the actual schema must be established and separately
validated before the component can be accepted.

Parent6 and Jafly20 remain pinned. A future Jafly23 adoption requires actual Central
publication proof first. Parent21 and BOM7 remain unreserved by this source
change. Any later allocation must reconcile published versions and actual
account-visible staged deployments before separately reviewed release actions.

Status reconciliation validates the exact listed deployment ID and state, bounded
nonempty canonical namespace PURLs, uniqueness, and inclusion of any already known
listing coordinates. The sanitized listing and status observations must match in
two sweeps under the same outer worker deadline and total component budget.
Neither the official status response schema nor its examples assert an exhaustive
mapping from status PURLs to listing rows with missing PURLs. Those rows therefore
remain `INCOMPLETE` / `PURL_MISSING`, with `PURL_COVERAGE_UNPROVEN`; a nonempty status
PURL array never authorizes allocation or silently reclassifies an unknown row.
Names, paths, errors, raw bodies, and credentials never supply missing coordinates.

This boundary follows the official [Publisher API](https://central.sonatype.com/api-doc)
and [status query documentation](https://central.sonatype.org/publish/publish-portal-api/#checking-status).
A separately established complete coordinate contract is still required to resolve
unknown-row coverage. This source change does not establish authenticated account
visibility, historical reservation completeness, or a successful Portal query.
