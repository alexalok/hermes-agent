# CI on forks

`ci.yaml` and its reusable workflows select runners from `github.repository`.
Independently triggered PR workflows use the same policy, including Nix checks,
Docker preflight, plugin admission, native payload/SDK checks, Termux checks,
and label-driven reruns.
`NousResearch/hermes-agent` keeps its Blacksmith and Windows ARM larger runners.
Other repositories use standard GitHub-hosted runners without configuring a
runner pool or adding credentials:

| Platform | Fork runner |
| --- | --- |
| Linux x64 | `ubuntu-24.04` |
| macOS unit tests | Disabled on forks; both upstream slices remain |
| Native payload macOS | `macos-15`, `macos-15-intel` (existing standard runners) |
| Windows x64 | `windows-2025` |
| Windows arm64 | `windows-11-arm` |

The caller repository owns the runner allocation even in a reusable workflow.
A fork-authored PR tested in the upstream repository therefore still uses
upstream runners. The comparison uses the complete repository name, not the PR
head owner or just the organization name.

Forks omit the macOS-only unit test suite because standard macOS ARM runners
have repeatedly failed to acquire capacity. Upstream keeps both macOS slices
on Blacksmith; independent native payload builds still cover macOS on the
existing standard runners. All other lanes, test selectors, permissions, and
result gates remain enabled.
Jobs normally hosted on larger runners get at least a 120-minute job budget on
standard runners; existing longer budgets remain. Linux unit tests use the
canonical runner's CPU-count default. Windows
unit tests retain one worker per two CPUs; process-heavy E2E lanes use one worker
on forks. This preserves headroom for child processes instead of filling the
smaller VM with concurrent process trees. Fork JS checks run one workspace check
at a time because each check can use its own CPU-sized worker pool; the scheduler
still runs every check and reports all failures. Per-test and per-file deadlines
stay unchanged. Fork runs will take longer, especially with cold caches.

The pinned `useblacksmith/checkout` action is portable: its
[`shouldUseBlacksmithCache` implementation](https://github.com/useblacksmith/checkout/blob/25227e61ff9dafe400e22fa487b673eac4e4409a/src/blacksmith-cache.ts#L162)
returns false without `BLACKSMITH_VM_ID` and performs a standard checkout.
The CI graph's nested setup actions already use `actions/cache`. No Blacksmith
service or token is needed on GitHub-hosted runners.

This policy covers the workflow graph rooted at `ci.yaml` and jobs eligible on
fork PRs in every independently triggered `pull_request` workflow. Existing
upstream-only build, release, credential, and publication gates remain intact.
Release-only and maintenance workflows retain their own infrastructure policy.

`tests/ci/test_fork_runners.py` traverses that graph, discovers independent PR
workflows, and evaluates runner and worker expressions for upstream and fork
callers. It checks standard-runner
availability, OS/architecture parity for enabled lanes, worker load per CPU, and
upstream-only macOS unit-test selection. Existing routing and
required-result tests cover lane selection and failure propagation.

References: [GitHub runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[reusable workflow runner context](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#how-reusable-workflows-use-runners).
