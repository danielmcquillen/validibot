# Deploy to AWS

AWS is not a first-class Validibot deployment target yet.

There is a `just aws ...` module in the repo, but it is currently a stub and does not provide a supported end-to-end deployment workflow.

## Current status

Today, the AWS target should be treated as planned rather than implemented.

That means:

- there is no documented `just aws` bootstrap flow
- there is no maintained AWS infrastructure guide in this repo
- there is no supported AWS release workflow comparable to GCP

## Best option on AWS today

If you need to run Validibot on AWS right now, the practical path is:

1. provision an EC2 or Lightsail host you control
2. follow [Deploy with Docker Compose](deploy-docker-compose.md)
3. add your own reverse proxy and backups

That gives you a supported Validibot deployment path while still letting you host on AWS infrastructure.

## What a future AWS target would need

A proper AWS deployment guide would need to cover:

- compute for web and worker services
- PostgreSQL
- object storage
- secrets management
- scheduler/background job equivalents
- container registry and deploy automation

Until that exists, Docker Compose on an AWS-managed VM is the recommended route.

## Recommended alternatives

- Choose [Deploy with Docker Compose](deploy-docker-compose.md) for the current supported single-host path
- Choose [Deploy to GCP](deploy-gcp.md) if you want the currently supported managed cloud path
