# Trivy config-scan ignore policy for VIATOR.
#
# Activated by: trivy config --ignore-policy ci/trivy-config-ignore.rego <path>
# Currently NOT wired into the CI workflow — reserved for the future
# `trivy config docker/docker-compose.yml` step. See ci/README.md.
#
# When we enable it, the rule below will whitelist the worker container's
# docker-socket mount (intentional design, documented in
# VIATOR-technical-spec.md §10.1).

package trivy

import future.keywords.if

default ignore := false

# Example rule (commented until wired in):
# ignore if {
#     input.AVDID == "AVD-DS-0026"            # Docker socket mount
#     input.Service == "worker"
# }
