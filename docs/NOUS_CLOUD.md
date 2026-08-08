# Running in a Nous (Hermes Agent) cloud sandbox

[Hermes Agent](https://hermes-agent.nousresearch.com/) is Nous Research's
AI agent. When it runs shell commands, it does so inside a "terminal
backend" - a sandbox that can be `local`, `docker`, `ssh`, or a cloud
backend (`modal`, `daytona`, `vercel_sandbox`, `singularity`). That's what
"Nous cloud" refers to here: not a batch/job platform like AWS Batch, but a
persistent sandbox container that Hermes execs many separate commands into
over the course of a conversation.

This is a different shape than [`AWS_DEPLOY.md`](AWS_DEPLOY.md)'s
self-terminating EC2 instance (one container, one command, run to
completion, then gone). A Hermes sandbox instead stays up and idle between
commands, so:

- The image must **not** auto-run the downloader and exit - it needs to sit
  there so Hermes can run `pip install`-equivalent setup once, then issue
  the actual download command(s) separately, possibly across many turns.
- That's what [`Dockerfile.sandbox`](../Dockerfile.sandbox) is for: same
  deps and code as the main `Dockerfile`, but `CMD ["sleep", "infinity"]`
  instead of an entrypoint that launches the pipeline immediately. Build it
  with:

  ```bash
  docker build -f Dockerfile.sandbox -t achem-tempo-sandbox .
  ```

  For a cloud backend (Modal, Daytona) rather than a local Docker daemon,
  push this image to a registry the backend can pull from (Docker Hub,
  ECR, etc.) instead of just building it locally.

## 1. Credentials

Same two credential sets as the AWS deployment - NASA Earthdata login and
AWS (for the S3 bucket that's the pipeline's source of truth) - but
supplied to Hermes rather than to EC2/Secrets Manager. Put them in
`~/.hermes/.env` on the machine running Hermes (never in `config.yaml`,
and never in this repo):

```
EARTHDATA_USERNAME=matthew0011c0
EARTHDATA_PASSWORD=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-west-2
```

`docker-entrypoint.sh` isn't used by the sandbox image (there's no
entrypoint), so `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD` need to be turned
into `~/.netrc` inside the sandbox by running
`scripts/setup_earthdata_auth.py` (interactive) or
`scripts/use_earthdata_account.py <name>` (reads
`secrets/earthdata_accounts.json`) as the first command once the sandbox is
up, or by having the agent write `~/.netrc` directly from the forwarded env
vars the same way `docker-entrypoint.sh` does.

## 2. `~/.hermes/config.yaml`

```yaml
terminal:
  backend: docker
  docker_image: "achem-tempo-sandbox:latest"
  docker_forward_env:
    - EARTHDATA_USERNAME
    - EARTHDATA_PASSWORD
    - AWS_ACCESS_KEY_ID
    - AWS_SECRET_ACCESS_KEY
    - AWS_DEFAULT_REGION
  container_persistent: true   # keep /app state across commands in one session
  container_disk: 10240        # MB - see disk note below; raise if the backend allows more
  timeout: 1800                 # a single month's Harmony job + regrid can run long
```

For `modal`/`daytona`/`vercel_sandbox` instead of local `docker`, swap
`backend:` and add that backend's required token/API key env var (see
Hermes's [environment variables
reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables));
`docker_image` still names the image to run, now pulled from wherever you
pushed it in step 0.

## 3. Running the pipeline

Once the sandbox is up, tell Hermes to run (first time only):

```bash
python scripts/use_earthdata_account.py matthew0011c0   # or setup_earthdata_auth.py
```

then the actual backfill:

```bash
python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 \
  --delete-local-after-upload
```

**Use `--delete-local-after-upload`.** Unlike the EC2 deployment (a fresh
disk per run, thrown away on termination), a persistent Hermes sandbox
keeps every downloaded `.nc`/`.tif` on the same container disk for the
whole session. Cloud backends cap that disk - Daytona tops out at 10 GiB -
and a multi-year backfill will fill it long before finishing. This flag
deletes each month's local files right after they're confirmed uploaded to
S3, which is already the pipeline's source of truth for resuming, so
nothing is lost; a re-run (from this sandbox or a completely fresh one)
still skips any month already in the bucket via the existing bucket-first
check.

If a run is interrupted or the sandbox is recycled, just re-run the same
command - already-uploaded months are recognized from the bucket exactly
as in the AWS deployment.
