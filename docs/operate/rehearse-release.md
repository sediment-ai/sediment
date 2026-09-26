# Review release verification

Before installing a published release, review its verification results and
artifact identities. Operators don't need to produce the release artifacts.

## Check the release

1. Select a [published release](https://github.com/sediment-ai/sediment/releases)
   and retain its version, `SHA256SUMS`, and security evidence.
2. Follow [Check release and deployment security](security.md) to compare that
   evidence with the packages you install.
3. Verify your installation with the [deployment checks](deploy.md#5-verify-the-deployment)
   and the [pilot procedure](run-pilot.md).

Release verification uses synthetic inputs. It doesn't verify your credentials,
network, private repositories, or live agent delivery.

## Maintainer procedure

Release maintainers use [Release rehearsal and publication](../../CONTRIBUTING.md#release-rehearsal-and-publication).
