terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = "freedomridge" }
  }
}

data "aws_vpc" "default" { default = true }
data "aws_ec2_instance_type_offerings" "app" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
  filter {
    name   = "availability-zone"
    values = var.availability_zone == null ? data.aws_ec2_instance_type_offerings.app.locations : [var.availability_zone]
  }
  lifecycle {
    postcondition {
      condition     = length(self.ids) > 0 && (var.availability_zone == null ? true : contains(data.aws_ec2_instance_type_offerings.app.locations, var.availability_zone))
      error_message = "Choose a default public subnet AZ offering the selected instance type."
    }
  }
}
data "aws_subnet" "selected" {
  id = sort(data.aws_subnets.default.ids)[0]
}
# Canonical publishes its current Ubuntu 26.04 LTS server image (gp3) for each architecture under
# these public parameters in every region; the instance type's architecture picks one (the release
# images are built for both). The x86_64 images need Haswell-class CPUs or newer (t3 and c7i
# qualify; M4/C4/R4 and older families do not).
data "aws_ec2_instance_type" "app" {
  instance_type = var.instance_type
}
locals {
  ubuntu_arch = contains(data.aws_ec2_instance_type.app.supported_architectures, "arm64") ? "arm64" : "amd64"
}
data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/resolute/stable/current/${local.ubuntu_arch}/hvm/ebs-gp3/ami-id"
}

resource "aws_security_group" "freedomridge_sg" {
  name_prefix = "freedomridge-"
  description = "HTTPS app, HTTP ACME/redirect, operator-only SSH"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    description = "HTTP ACME and redirect"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "Operator SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.selected.availability_zone
  size              = var.data_volume_gib
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "freedomridge-data" }
  # Explicitly remove this guard only after a tested backup, for final teardown.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [availability_zone]
  }
}
resource "aws_ebs_volume" "cache" {
  availability_zone = data.aws_subnet.selected.availability_zone
  size              = var.cache_volume_gib
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "freedomridge-cache" }
  lifecycle { ignore_changes = [availability_zone] }
}

resource "aws_s3_bucket" "backups" {
  bucket_prefix = "freedomridge-backups-"
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "expire-dumps"
    status = "Enabled"
    filter { prefix = "database/" }
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Dataset media for imports run with --media-uri (AWS guide, section 8). The instance only reads
# media/; imports upload with the operator's own AWS login, never the instance role.
resource "aws_s3_bucket" "media" {
  bucket_prefix = "freedomridge-media-"
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    id     = "media"
    status = "Enabled"
    filter { prefix = "media/" }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  # Retained dataset archives, needed only to re-run a download or import (section 13).
  rule {
    id     = "archives-glacier-ir"
    status = "Enabled"
    filter { prefix = "archives/" }
    transition {
      days          = 0
      storage_class = "GLACIER_IR"
    }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_policy" "media" {
  bucket = aws_s3_bucket.media.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.media.arn, "${aws_s3_bucket.media.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_iam_role" "instance" {
  name_prefix = "freedomridge-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "instance" {
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.backups.arn,
      Condition = { StringLike = { "s3:prefix" = ["database", "database/*"] } } },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:AbortMultipartUpload"],
      Resource = "${aws_s3_bucket.backups.arn}/database/*" },
      # The backend serves s3:// media; it can read media/ only, never write. Listing (key names only)
      # makes a missing key a 404 instead of an access-denied error.
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.media.arn },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.media.arn}/media/*" },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*",
      Condition = { StringEquals = { "cloudwatch:namespace" = "FreedomRidgeHost" } } }
    ]
  })
}
# On-demand compute workers (docs/ON_DEMAND_WORKERS.md): start EC2 machines tagged fridge:launch, then look at and
# terminate only those. The machines get no role of their own; Amazon Linux comes from AWS's public SSM parameter.
resource "aws_iam_role_policy" "on_demand_workers" {
  count = var.on_demand_workers ? 1 : 0
  role  = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect   = "Allow", Action = ["ec2:RunInstances"],
        Resource = ["arn:aws:ec2:${var.aws_region}:*:instance/*", "arn:aws:ec2:${var.aws_region}:*:volume/*"],
      Condition = { StringLike = { "aws:RequestTag/fridge:launch" = "*" } } },
      { Effect = "Allow", Action = ["ec2:RunInstances"],
        Resource = [
          "arn:aws:ec2:${var.aws_region}::image/*", "arn:aws:ec2:${var.aws_region}:*:subnet/*",
          "arn:aws:ec2:${var.aws_region}:*:network-interface/*", "arn:aws:ec2:${var.aws_region}:*:security-group/*",
          "arn:aws:ec2:${var.aws_region}:*:spot-instances-request/*",
      ] },
      { Effect   = "Allow", Action = ["ec2:CreateTags"],
        Resource = ["arn:aws:ec2:${var.aws_region}:*:instance/*", "arn:aws:ec2:${var.aws_region}:*:volume/*"],
      Condition = { StringEquals = { "ec2:CreateAction" = "RunInstances" } } },
      { Effect = "Allow", Action = ["ec2:TerminateInstances"], Resource = "arn:aws:ec2:${var.aws_region}:*:instance/*",
      Condition = { StringLike = { "aws:ResourceTag/fridge:launch" = "*" } } },
      { Effect = "Allow", Action = ["ec2:DescribeInstances", "ec2:DescribeInstanceTypes"], Resource = "*" },
      { Effect = "Allow", Action = ["ssm:GetParameters"],
      Resource = "arn:aws:ssm:${var.aws_region}::parameter/aws/service/ami-amazon-linux-latest/*" }
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name_prefix = "freedomridge-"
  role        = aws_iam_role.instance.name
}

resource "aws_instance" "freedomridge_app" {
  ami                         = coalesce(var.ami_id, data.aws_ssm_parameter.ubuntu_ami.insecure_value)
  instance_type               = var.instance_type
  key_name                    = var.key_name
  subnet_id                   = data.aws_subnet.selected.id
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.freedomridge_sg.id]
  iam_instance_profile        = aws_iam_instance_profile.instance.name
  monitoring                  = true
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # IMDSv2 instance role usable from backup container
    http_endpoint               = "enabled"
  }
  root_block_device {
    volume_size = 40 # OS, Docker images/build layers and importer virtualenv
    volume_type = "gp3"
    encrypted   = true
  }
  user_data = templatefile("${path.module}/user_data.sh", {
    repository_url_base64        = base64encode(var.repository_url)
    repository_ref_base64        = base64encode(var.repository_ref)
    repository_token_file_base64 = base64encode(var.repository_token_file)
    public_origin                = var.public_origin
    email_base64                 = base64encode(var.budget_email)
    aws_region                   = var.aws_region
    data_volume_id               = aws_ebs_volume.data.id
    cache_volume_id              = aws_ebs_volume.cache.id
    backup_s3_uri                = "s3://${aws_s3_bucket.backups.id}/database"
    media_bucket                 = aws_s3_bucket.media.id
  })
  # AMI/user-data drift and subnet ordering must not replace or restart production.
  # Resizes are deliberate stop/modify/start operations outside Terraform.
  lifecycle {
    ignore_changes  = [ami, user_data, subnet_id, instance_type]
    prevent_destroy = true
  }
  tags = { Name = "Freedom-Ridge-App-Instance" }
}
resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.freedomridge_app.id
}
resource "aws_volume_attachment" "cache" {
  device_name = "/dev/sdg"
  volume_id   = aws_ebs_volume.cache.id
  instance_id = aws_instance.freedomridge_app.id
}
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.freedomridge_app.id
}

resource "aws_sns_topic" "alerts" { name_prefix = "freedomridge-" }
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.budget_email
}
resource "aws_cloudwatch_metric_alarm" "cpu" {
  alarm_name          = "freedomridge-cpu-${aws_instance.freedomridge_app.id}"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = { InstanceId = aws_instance.freedomridge_app.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "missing"
}
resource "aws_cloudwatch_metric_alarm" "disk" {
  for_each            = toset(["/", "/data", "/cache"])
  alarm_name          = "freedomridge-disk-${replace(each.key, "/", "-")}-${aws_instance.freedomridge_app.id}"
  namespace           = "FreedomRidgeHost"
  metric_name         = "disk_used_percent"
  dimensions          = { InstanceId = aws_instance.freedomridge_app.id, path = each.key }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "breaching"
}
resource "aws_budgets_budget" "monthly" {
  name         = "freedomridge-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
