# corpus-generated

Real tool output from real tools, on small projects written to fail in
chosen ways. The logs are genuine (Maven, tsc, Terraform and kubectl ran
and printed them), but the failures were picked by someone who knows
pare's heuristics, and the keep lists were written by the same person.
That makes this a second development corpus, useful for checking that
segmentation and severity detection hold on formats `corpus/` does not
cover. It is **not** the held-out set; `corpus-heldout/README.md` has the
protocol for that, and nothing here counts toward it.

| case | tool | what the log shows |
|---|---|---|
| maven-compile | Maven 3 / javac 17 | cold cache (400 download lines), core module builds, api module fails with three compile errors, reactor summary |
| maven-test | Maven 3 / Surefire 3.2.5 / JUnit 5 | one assertion failure and one NullPointerException with stack traces, results block, build failure |
| tsc-build | TypeScript 5.6 via `npm run build` | 35 errors across 10 files, including multi-line nested messages |
| terraform-init | Terraform 1.9.8 | provider registry unreachable (the container's proxy; the shape of any offline `init`) |
| terraform-validate | Terraform 1.9.8 | undeclared variables and a bad function call, with source context blocks |
| terraform-apply | Terraform 1.9.8 | a real plan and apply of 20 built-in `terraform_data` resources with three failed `check` blocks (warnings) |
| kubectl-apply | kubectl 1.31 | `apply`, `rollout status` and `get pods` against an unreachable API server |

Not captured here, because the container cannot reach the registries or
run a cluster: a Terraform plan with a real provider diff, kubectl output
from a live cluster (rollouts, events, pod logs), and an Android Gradle
build. Those have to come from a developer machine.

Files follow the `corpus/` convention: `<name>.log`, `<name>.keep` (lines
that must survive, substring match), `<name>.cmd` (the command). Paths are
scrubbed to `/home/dev/proj`; the JVM's `Picked up JAVA_TOOL_OPTIONS`
line, an artifact of the capture container, is removed.

## Regenerating

The projects are in `src/`. Build outputs (`target/`, `node_modules/`,
`.terraform/`, state files) are ignored by git.

```
cd src/maven-proj && mvn -B -Dmaven.test.skip=true package   # maven-compile (cold ~/.m2 for the download lines)
cd src/maven-proj && mvn -B test                              # maven-test
cd src/tsc-proj && npm install && npm run build               # tsc-build
cd src/terraform-proj/infra && terraform init -input=false -no-color          # terraform-init (needs the registry blocked)
cd src/terraform-proj/config && terraform init -input=false -no-color && terraform validate -no-color
cd src/terraform-proj/stack && terraform init -input=false -no-color && terraform apply -auto-approve -input=false -no-color
cd src/kubectl-proj && KUBECONFIG=$PWD/kubeconfig kubectl apply -f k8s/      # then rollout status, get pods
```

```
.venv/bin/python bench/run.py --corpus corpus-generated [--budget 600] [--show-misses]
```
