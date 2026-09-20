# TREVol / Macro Cocina Fit — AWS infrastructure

Static site (S3 + CloudFront) + serverless order backend
(API Gateway + Lambda + DynamoDB). No EC2, no ALB, no NAT Gateway.
Estimated cost running all month: **$0.50 – $1.50**, with an alarm at $3.

## Install Terraform (once, on your machine — not in this chat)

```bash
brew install terraform          # Mac
# or: https://developer.hashicorp.com/terraform/install
terraform version                # confirm it ran
```

You also need the AWS CLI configured with your credentials:
```bash
aws configure
```

## Step 1 — Bootstrap (ONLY the first time)

Creates the state bucket and the lock table. This uses local state on purpose.

```bash
cd bootstrap
terraform init
terraform apply
```

Confirm the output names (`trevol-terraform-state`,
`trevol-terraform-lock`) match what's written in
`envs/prod/providers.tf`. If you changed `project` from the default
`trevol`, adjust that file by hand.

## Step 2 — Configure variables

```bash
cd ../envs/prod
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`: put your real email in `alert_email` (that's
where spend alerts arrive). Leave `domain_active = false` for now.

## Step 3 — First apply (no domain yet)

```bash
terraform init
terraform apply
```

When it finishes, the `site_url` output gives you a URL like
`https://d111111abcdef8.cloudfront.net` — the site is already up and
running there, order form included.

## Step 4 — Once the AWS email confirming the domain arrives

Edit `terraform.tfvars`:
```hcl
domain_active = true
```

```bash
terraform apply
```

This connects Route 53, validates the HTTPS certificate automatically
(can take a few minutes on the first apply while ACM validates), and
moves the site to `https://trevolcamaguey.com`.

## Updating the page content

Just edit the files under `/site` and run again:
```bash
terraform apply
```
Terraform only uploads what changed (compared by hash).

## Tearing it all down (to spend nothing while it's not in use)

```bash
cd envs/prod
terraform destroy
```
The bootstrap (state bucket) stays — never destroy it unless you want
to abandon the project entirely.

## Structure

```
bootstrap/        S3 bucket + DynamoDB table for Terraform state (once)
envs/prod/        The real "entrypoint": composes every module
modules/frontend/ S3 + CloudFront (the site)
modules/dns/      Route 53 + ACM certificate (HTTPS)
modules/backend-api/ DynamoDB + Lambda + API Gateway (the order form)
modules/budget/   AWS Budgets alarm at $3/month
lambda/order_handler/ Python source for the Lambda that saves orders
site/             HTML/CSS/JS for the site — this is what gets uploaded to S3
```
