<a href="https://fireflysoftware.io/openplatform/">
<picture>
  <source media="(max-width: 640px) and (prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/firefly-oss/.github/main/profile/assets/banner-narrow-dark.svg">
  <source media="(max-width: 640px)" srcset="https://raw.githubusercontent.com/firefly-oss/.github/main/profile/assets/banner-narrow-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/firefly-oss/.github/main/profile/assets/banner-dark.svg">
  <img src="https://raw.githubusercontent.com/firefly-oss/.github/main/profile/assets/banner-light.svg" alt="Firefly OpenPlatform - Platform - Parent" width="100%">
</picture>
</a>

# Firefly OpenPlatform - Platform - Parent

Developed by the Firefly Software Foundation. [dev@fireflysoftware.io](mailto:dev@fireflysoftware.io)

[Website](https://fireflysoftware.io/) · [OpenPlatform](https://fireflysoftware.io/openplatform/) · [Module catalogue](https://fireflysoftware.io/openplatform/modules/)

Parent POM for all **Firefly OpenPlatform** modules. It extends `org.fireflyframework:fireflyframework-parent` (which already manages Spring Boot, Spring Cloud, Lombok, MapStruct, and all other third-party dependency and plugin versions) and adds the Firefly-specific configuration on top. Banking, lending, and insurance modules share this parent.

## What It Provides

| Concern | Description |
|---------|-------------|
| **Firefly Framework BOM** | Imports `org.fireflyframework:fireflyframework-bom` so that all `org.fireflyframework` artifact versions are managed centrally |
| **Framework version** | Single source of truth for `fireflyframework.bom.version` (currently **26.10.23**, Spring Boot 4.1.1) |
| **Project conventions** | Shared OpenAPI code-gen properties (`base.package`, model/api package layout) |

All third-party dependency versions (Spring Boot, Lombok, MapStruct, database drivers, testing frameworks, etc.) and plugin management are inherited from `fireflyframework-parent`. There is no need to redeclare them here.

## One framework release train

The framework parent and framework BOM select the same published Jafly version, **26.10.23**. All framework modules, adapters, batch capabilities, and published SDK artifacts share that release train; services inherit their versions. The CI caller guard rejects a mismatched parent/BOM pair. This source alignment does not publish a corrected OpenPlatform parent or qualify service adoption: the immutable published OpenPlatform parent26.10.6 still requires a new release and coordinated consumer updates.

## Inheritance Chain

```
org.fireflyframework:fireflyframework-parent  (Spring Boot, Lombok, plugins, …)
  └── io.fireflysoftware:firefly-parent              (fireflyframework BOM + project conventions)
        └── your module                       (inherits everything)
```

## Usage

Reference `firefly-parent` as the parent in your module's `pom.xml`. This excerpt shows the current source coordinates; the published `26.10.6` POM does not retain this inheritance contract. Consumer adoption requires a separately approved corrected parent release, as described below.

```xml
<parent>
    <groupId>io.fireflysoftware</groupId>
    <artifactId>firefly-parent</artifactId>
    <version>26.10.6</version>
    <relativePath/>
</parent>
```

Your module then inherits:
- All `org.fireflyframework` dependency versions (via the BOM at **26.10.23**)
- All third-party dependency and plugin versions from `fireflyframework-parent`
- Firefly OpenAPI code-generation conventions

No need to declare `fireflyframework.bom.version` or import the `fireflyframework-bom` in your module — the parent handles it.

**Maven Central** hosts the framework parent/BOM `26.10.23` (`https://repo1.maven.org/maven2/org/fireflyframework/fireflyframework-parent/26.10.23/`). Their availability does not prove that the separately published OpenPlatform `firefly-parent:26.10.6` works: that artifact lost the build-parent metadata during flattening. Adding a repository or installing the same OpenPlatform coordinates locally does not qualify the broken published parent. A new corrected parent release and coordinated consumer updates are required.

## Published parent contract

The release profile uses Flatten Maven Plugin 1.7.0's `resolveCiFriendliesOnly` mode because this artifact is a build parent. The published POM must retain its parent chain, properties, imported dependency management, and inherited plugin management. The previous `ossrh` mode removed that contract. Existing release-profile inheritance is intentional: descendant publication POMs must remain usable through the retained parent chain too.

The already published `26.10.6` artifact is immutable. This source correction requires a separately approved new parent version and coordinated consumer parent-version updates; do not republish `26.10.6` or treat a local same-version install/cache replacement as consumer qualification. The source version remains unchanged in this checkpoint, and no release is authorized by the regression script.

Run the bounded file-only regression with Python 3.11 or newer, an existing Maven 3.9.16 distribution, and JDK 25.0.3:

```bash
python3 tests/verify_parent_contract.py \
  --maven /path/to/apache-maven-3.9.16/bin/mvn \
  --java-home /path/to/jdk-25.0.3 \
  --output /absolute/path/to/new-owned-evidence
```

When omitted, `--maven` uses `MAVEN_EXECUTABLE` or `mvn` on `PATH`; `--java-home` uses `JAVA_HOME`. The resolver checks Maven's distribution metadata and the JDK release file before running a model check. It supports macOS and Linux with their platform JVM libraries and rejects unsupported layouts/platforms. It records resolved paths and launcher/distribution/JDK hashes in `tool-resolution.json` and `tool-pins.json`. It never installs or downloads Maven or a JDK.

The script retains the original Git-source `ossrh` failure and corrected-mode results in separate directories. It runs direct flatten/effective-model goals against genuine Maven consumers using explicit relative paths to the generated publication POMs, including a flattened descendant. It uses a new Central-only, credential-free dependency cache, bounded processes and output sizes, and never invokes install, deploy, release lifecycle, signing, applications, databases, or listeners. Existing evidence directories are preserved. The generated POMs are file-only test artifacts, not replacement repository coordinates.

## Properties

| Property | Value | Purpose |
|----------|-------|---------|
| `fireflyframework.bom.version` | `26.10.23` | Version of the Firefly Framework BOM (Boot 4.1.1 train) |
| `base.package` | `io.fireflysoftware` | Root Java package for all modules |
| `openapi.base.package` | `${base.package}.${project.artifactId}` | OpenAPI code-gen base package |
| `openapi.model.package` | `…interfaces.dto` | Generated DTO package |
| `openapi.api.package` | `…interfaces.api` | Generated API package |
| `openapi.gen.skip` | `true` | Controls OpenAPI spec generation (set to `false` in `-web` modules to enable) |

## Automated OpenAPI Generation

This parent POM includes a shared Maven profile (`generate-openapi`) that automates OpenAPI spec generation and SDK creation at build time. The profile is always active but skipped by default — each `-web` module opts in by setting `openapi.gen.skip=false`.

### How It Works

During `mvn install`, the profile:

1. **`pre-integration-test`** — Starts a lightweight Spring Boot app (`OpenApiGenApplication`) that loads only the controllers and Springdoc
2. **`integration-test`** — Fetches the OpenAPI spec from the running app and writes it to `target/openapi/openapi.yml`
3. **`post-integration-test`** — Stops the app

The `-sdk` module then uses `openapi-generator-maven-plugin` to generate typed API clients from the spec.

### Enabling in a Microservice

**Step 1:** Add two properties to the `-web` module's `pom.xml`:

```xml
<properties>
    <openapi.gen.skip>false</openapi.gen.skip>
    <openapi.gen.mainClass>io.fireflysoftware.your.module.web.openapi.OpenApiGenApplication</openapi.gen.mainClass>
</properties>
```

**Step 2:** Create `OpenApiGenApplication` in `src/test/java` (never packaged into production):

```java
@EnableOpenApiGen
@ComponentScan(basePackages = "io.fireflysoftware.your.module.web.controllers")
public class OpenApiGenApplication {
    public static void main(String[] args) {
        SpringApplication.run(OpenApiGenApplication.class, args);
    }
}
```

`@EnableOpenApiGen` and `AutoMockMissingBeansConfig` are provided by `fireflyframework-web`. See the [fireflyframework-web README](https://github.com/fireflyframework/fireflyframework-web) for details on those components.

**Step 3:** Run the build:

```bash
mvn clean install
```

The generated spec will be at `your-module-web/target/openapi/openapi.yml`. To skip generation temporarily, pass `-DskipOpenApiGen`.

### Profile Properties

| Property | Default | Purpose |
|----------|---------|---------|
| `openapi.gen.skip` | `true` | Set to `false` in `-web` modules to enable generation |
| `openapi.gen.mainClass` | _(none)_ | Fully qualified class name of the `OpenApiGenApplication` |

## Consuming jafly 26.10.23 (OpenPlatform)

| Item | Value |
|------|-------|
| Framework parent / BOM | `org.fireflyframework:fireflyframework-parent` / `fireflyframework-bom` **26.10.23** |
| Spring Boot | **4.1.1** |
| Spring Cloud | **2025.1.3** |
| Java | **25** (`JAVA_HOME` must be JDK 25; Maven on JDK 26 will fail toolchain/source checks) |
| Registry | **Maven Central first** — no extra `<repository>` required when Central is reachable |

### Resolve the framework from Central

```xml
<!-- Already done by firefly-parent — do not re-pin in service POMs -->
<parent>
    <groupId>org.fireflyframework</groupId>
    <artifactId>fireflyframework-parent</artifactId>
    <version>26.10.23</version>
    <relativePath/>
</parent>
```

Central coordinates: `https://repo1.maven.org/maven2/org/fireflyframework/fireflyframework-parent/26.10.23/`.

### Dual-repo profile (optional GitHub Packages)

Activate only when Central is unreachable (air-gapped / pre-sync CI):

```bash
mvn -Pjafly-packages …   # server id firefly-github, PAT with read:packages
```

Prefer Central for **26.10.23+**. Leave `-Pjafly-packages` off by default. Consumer qualification uses the verified published framework artifacts. A local framework source build is development evidence and must not replace published coordinates in the qualification cache.

### Boot 4 / OpenAPI regeneration notes

When bumping a service onto this parent:

1. Adopt a separately approved corrected parent release and update the service's parent version. Do not install this source checkpoint under the already published `firefly-parent:26.10.6` coordinates as a consumer workaround; the framework BOM remains pinned to **26.10.23**.
2. In each `-sdk` module: drop hardcoded `openapi-generator.version` / Jackson / swagger pins; inherit plugin + dependency versions from `fireflyframework-parent`; set `useJakartaEe=true` on the webclient generator; keep `javax.annotation-api` only while the generator still emits `javax.annotation.Generated`; add `spring-webflux` for the WebClient SDK runtime.
3. Apply Boot 4 config migrations from the [jafly Boot 4 guide](https://github.com/fireflyframework/fireflyframework-jafly/blob/main/docs/migration/boot4-application-guide.md) where used: `spring.jackson2.*` (not `spring.jackson.*`), OTLP tracing keys, Testcontainers 2 module names, springdoc 3.x.
4. Regenerate OpenAPI via the parent `generate-openapi` profile (`openapi.gen.skip=false` on `-web`); skip with `-DskipOpenApiGen` when iterating compile-only.
5. Verify with JDK 25: `mvn -B -ntp clean test`.

### Roll-out order

Prefer dependency order **core → domain → exp** for the same capability:

1. Shared libraries (`library-*`) and `core-common-*`
2. Capability cores (`core-lending-*`, `core-banking-*`, …)
3. Domain aggregators (`domain-*`)
4. Experience APIs (`exp-*`)

### Roll-out status (2026-10-18/19)

This historical source-migration snapshot is not verification of the published `firefly-parent:26.10.6` consumer contract or of current service builds. The published-parent correction and subsequent consumer qualification remain separate requirements.

**Train:** jafly **26.10.20** / Spring Boot **4.1.1** / Java **25** — consumed from **Maven Central** (optional `-Pjafly-packages` fallback).

| Layer | Status | Notes |
|-------|--------|-------|
| `firefly-parent` | **Green** | Parent + BOM pin `26.10.20` |
| `library-common-batch` | **Green** | Boot 4 Flyway auto-config fixes |
| `core-common-*` | **Green** | All SDK modules unpinned + verified |
| `core-lending-*` | **Green** | All SDK modules unpinned + verified |
| `core-banking-*` | **Green** | Includes `core-banking-psdx` Boot 4 error/security fixes |
| `domain-*` (banking, lending, common, product, customer, distributor, core, data) | **Green** | SDK recipe + Boot 4 test fixes as needed |
| `exp-*` (accounts, cards, channel-management, distributor, lending, notifications, onboarding, profile, security) | **Green** | SDK recipe; WebFluxTest / `@MockitoBean` / reactive security excludes |
| `library-psps`, `library-rails` | **N/A (deferred)** | Still on `resilience4j-spring-boot3`; no OpenAPI SDK; out of this cutover |
| `app-authenticator`, `app-configurator-masters` | **N/A (deferred)** | Thin apps; bump with consumer cutover |

For a service using a separately qualified parent release, select its required JDK explicitly, for example: `JAVA_HOME=/path/to/jdk-25.0.3 mvn -B -ntp clean test -DskipOpenApiGen`. This disables the live OpenAPI profile, not the SDK generator; service-test results remain separate from the file-only parent regression.

## Related

- [fireflyframework-jafly](https://github.com/fireflyframework/fireflyframework-jafly) - Unified Firefly Framework (parent, BOM, libraries)
- [Boot 4 application migration guide](https://github.com/fireflyframework/fireflyframework-jafly/blob/main/docs/migration/boot4-application-guide.md)
- [firefly-bom](https://github.com/firefly-oss/firefly-bom) - Bill of Materials for all `io.fireflysoftware` artifacts

## Immutable release workflow adoption

Both release callers pin the reviewed shared workflow at `7b046ce67239bf889ab68e380614ba34723c9392` and load `.github/release-policy.json` from the exact tagged source. Dispatch must select an existing `vYY.MM.Patch` tag. The caller passes that ref's name; branch dispatches skip the release job. The shared gate verifies local and remote tag/source identity and refuses an existing GitHub release. Tag pushes validate only. Manual dispatch also defaults to `dry-run=true`; publication requires explicitly selecting false after the coordinated technical gates pass.

This repository has one POM project, no ordinary JUnit suites, and no producer or SDK. The explicit POM-only policy runs the normal `release` profile with `clean verify` and captures the effective model in the same invocation. The publication file is `.flattened-pom.xml`, the actual POM selected by that lifecycle. Source/Javadoc goals remain enabled; POM packaging produces no attached JARs. This exemption cannot qualify service reactors or their original producer/SDK gates.

The policy records the current source version `26.10.6`, which is already published. It does not authorize republishing it. A future release requires a separately reconciled and reserved numeric monthly patch, coordinated POM/policy updates, an immutable tag at the reviewed source, and fresh cold validation. No version is allocated here. The policy explicitly selects the independently verified prior publisher fingerprint; current hosted private-key access and publication rights still require their own gates. GitHub-only validation is the default route.

Offline caller controls require Python 3.11+, Ruby with its standard YAML/JSON libraries, and a clean checkout of the exact shared workflow implementation. No helper implementation is copied into this repository:

```bash
git clone https://github.com/fireflysoftwaresolutions/.github.git ../release-tools
git -C ../release-tools checkout --detach 7b046ce67239bf889ab68e380614ba34723c9392
PYTHONDONTWRITEBYTECODE=1 RELEASE_TOOLS_ROOT=../release-tools python3 -B -m unittest discover -s tests -p 'test_*.py' -v
actionlint .github/workflows/release.yml .github/workflows/maven-central.yml
```

Use a sibling checkout for `release-tools` when preparing a release source tree. These controls verify the caller contract and exercise the real pinned helper's positive/refusal behavior against synthetic files. They do not establish hosted workflow execution, signing, publication, tag/release creation, downloaded registry bytes, new parent consumer adoption, or full-platform acceptance. Cold Maven qualification must use a new empty owned cache and only already released dependencies; an unavailable dependency is a retained failure, never a reason to install upstream source under released coordinates.

## Read-only pull request validation

`pr-validation.yml` checks the exact PR source with read-only repository permission and the same immutable shared helper. It runs all Python controls, then the normal `release` profile's `clean verify` and effective-POM capture in one Maven invocation using new private settings and an empty owned dependency cache. The helper validates the policy, effective POM, test evidence, unchanged tracked source and exact frozen publication POM. Nonempty Maven launcher configuration requires separate review. The job records the actual caller/tool SHAs, numeric JDK/Maven versions and evidence hashes.

The artifact contains only bounded success or closed failure JSON; raw build output, effective XML, settings and cache remain private runner files. This CI path validates already declared POM coordinates without requiring or creating a release tag and never calls publication. A hosted passing run is separate evidence from local workflow controls, corrected parent consumer adoption, immutable release qualification, signing or registry publication.
