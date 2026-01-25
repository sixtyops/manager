# SixtyOps Website — Deployment

## Architecture

Static site served from S3 via CloudFront. No build step.

```
website/
├── index.html          # Single-page site (features, pricing, download)
├── billing.html        # Stripe Customer Portal page
├── 404.html            # Error page
├── infra.yml           # CloudFormation (S3 + CloudFront + clean URLs)
└── assets/
    ├── css/custom.css
    └── js/main.js
```

CDN dependencies (no npm/bundler): Tailwind CSS, Alpine.js, Google Fonts.

## Initial Setup

### 1. ACM Certificate

Request a certificate in **us-east-1** for `sixtyops.net` and `*.sixtyops.net`. Note the ARN.

```bash
aws acm request-certificate \
  --domain-name sixtyops.net \
  --subject-alternative-names "*.sixtyops.net" \
  --validation-method DNS \
  --region us-east-1
```

Validate via DNS, then grab the ARN.

### 2. Deploy Infrastructure

```bash
aws cloudformation deploy \
  --template-file website/infra.yml \
  --stack-name sixtyops-website \
  --parameter-overrides \
    DomainName=sixtyops.net \
    CertificateArn=arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT_ID \
  --region us-east-1
```

This creates:
- S3 bucket (private, CloudFront-only access via OAC)
- CloudFront distribution with HTTP/2+3, TLS 1.2+
- CloudFront Function for clean URLs (`/billing` → `/billing.html`) and www redirect
- Custom error pages (403/404 → `/404.html`)

### 3. DNS

Point your domain to the CloudFront distribution:

```
sixtyops.net        A     ALIAS → <distribution>.cloudfront.net
www.sixtyops.net    CNAME       → <distribution>.cloudfront.net
```

Get the distribution domain from stack outputs:

```bash
aws cloudformation describe-stacks --stack-name sixtyops-website \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionDomain'].OutputValue" \
  --output text
```

### 4. GitHub Actions Secrets

Add to your repo settings:

| Name | Type | Value |
|------|------|-------|
| `AWS_ROLE_ARN` | Secret | IAM role ARN with S3 + CloudFront permissions |
| `WEBSITE_S3_BUCKET` | Variable | `sixtyops.net` |
| `CLOUDFRONT_DISTRIBUTION_ID` | Variable | From stack outputs |

The workflow uses OIDC (no access keys). Create an IAM role that trusts `token.actions.githubusercontent.com` with:
- `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` on the bucket
- `cloudfront:CreateInvalidation` on the distribution

### 5. Initial Upload

```bash
aws s3 sync website/ s3://sixtyops.net/ \
  --delete \
  --exclude "DEPLOY.md" \
  --exclude "icon-preview.html" \
  --exclude "infra.yml" \
  --cache-control "max-age=3600"
```

## Deployment

Automatic on push to `main` when `website/` files change. See `.github/workflows/deploy-website.yml`.

Manual deploy: Actions → Deploy Website → Run workflow.

## Still Needs Hooking Up

- **Email**: `hello@sixtyops.net` needs a mailbox or forward
- **Billing portal**: Lambda for Stripe Customer Portal session creation
