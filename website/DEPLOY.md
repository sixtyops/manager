# SixtyOps Website — Deployment Guide

## Overview

Static single-page marketing site for sixtyops.net. No build step — plain HTML, CSS, and JS served directly from S3.

## Architecture

```
website/
├── index.html          # Single-page site (features, pricing, download)
├── billing.html        # Stripe Customer Portal page
├── 404.html            # Error page
└── assets/
    ├── css/custom.css   # Custom styles (mockup frames, device table, etc.)
    └── js/main.js       # Scroll animations + smooth scroll
```

### External Dependencies (CDN)

- **Tailwind CSS** — `cdn.tailwindcss.com` (runtime, no build)
- **Alpine.js** — `cdn.jsdelivr.net/npm/alpinejs` (FAQ accordion, mobile menu)
- **Inter + JetBrains Mono** — Google Fonts

No npm, no bundler, no build tools.

## S3 Deployment

### 1. Create S3 Bucket

```bash
aws s3 mb s3://sixtyops.net
```

### 2. Configure Static Website Hosting

```bash
aws s3 website s3://sixtyops.net \
  --index-document index.html \
  --error-document 404.html
```

### 3. Set Bucket Policy (public read)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicRead",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::sixtyops.net/*"
  }]
}
```

```bash
aws s3api put-bucket-policy \
  --bucket sixtyops.net \
  --policy file://bucket-policy.json
```

### 4. Upload Files

```bash
aws s3 sync website/ s3://sixtyops.net/ \
  --delete \
  --cache-control "max-age=3600"
```

### 5. CloudFront (recommended)

Create a CloudFront distribution pointing to the S3 website endpoint for HTTPS + custom domain.

```bash
# Point CloudFront to: sixtyops.net.s3-website-us-east-1.amazonaws.com
# Set alternate domain: sixtyops.net, www.sixtyops.net
# SSL certificate: Request via ACM in us-east-1
```

### 6. DNS (Route 53 or your registrar)

```
sixtyops.net        A     ALIAS → CloudFront distribution
www.sixtyops.net    CNAME       → CloudFront distribution
```

## What Still Needs Hooking Up

### Download Links

In `index.html`, the download buttons currently link to `#`. Replace with actual URLs:

```html
<!-- Find these two links in the #download section -->
<a href="#">Download OVA</a>   → Point to GitHub release or S3 presigned URL
<a href="#">Download QCOW2</a> → Point to GitHub release or S3 presigned URL
```

The GitHub releases already host appliance images at:
`https://github.com/isolson/firmware-updater/releases/tag/appliance-latest`

Example:
```html
<a href="https://github.com/isolson/firmware-updater/releases/download/appliance-latest/tachyon-appliance-a1.0-app1.2.0.ova">Download OVA</a>
```

### Free Tier Download Button

In the pricing section, the "Download Free" button also links to `#`. Point it to the same download URL as above.

### Pro Subscribe Button

The "Subscribe →" button in the pricing section links to `#`. Replace with a Stripe Payment Link:

```html
<a href="https://buy.stripe.com/YOUR_LINK_ID">Subscribe →</a>
```

### Billing Portal (billing.html)

The billing page has a placeholder that shows "Billing portal coming soon." To make it functional:

1. Create a Lambda function that accepts an email, looks up the Stripe customer, and creates a Customer Portal session
2. Deploy behind API Gateway
3. Uncomment the fetch call in `billing.html` (lines 83-92) and replace the endpoint URL

### Email Address

Contact email `hello@sixtyops.net` appears in the download section footer. Make sure this mailbox exists or forwards somewhere.

## Updating Content

Edit the HTML files directly — no build step needed. After changes:

```bash
aws s3 sync website/ s3://sixtyops.net/ --delete --cache-control "max-age=3600"

# If using CloudFront, invalidate cache:
aws cloudfront create-invalidation \
  --distribution-id YOUR_DIST_ID \
  --paths "/*"
```

## Design Notes

- **Theme**: "Engineering Precision" — minimal, teal + slate, monospace accents, thin rules between sections
- **Product mockups**: HTML/CSS recreations of actual SixtyOps dashboard UI inside browser chrome frames (`.app-frame`)
- **No images**: Everything is CSS — no image assets to manage
- **Responsive**: Tailwind breakpoints handle mobile. Mockup tables hide columns on small screens.
