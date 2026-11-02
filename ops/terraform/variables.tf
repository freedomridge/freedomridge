variable "aws_region" {
  description = "AWS region with a default VPC and public subnet."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "t3.large default (8 GiB; fits a $100/month budget). Use c7i.8xlarge only temporarily for imports; see the AWS guide."
  type        = string
  default     = "t3.large"
}

variable "key_name" {
  description = "Existing EC2 SSH key pair in this region."
  type        = string
}

variable "ami_id" {
  description = "Optional Ubuntu 26.04 x86_64 AMI override; otherwise resolve Canonical's current regional image."
  type        = string
  default     = null
}

variable "allowed_ssh_cidr" {
  description = "Required operator IPv4 CIDR, preferably your public IP/32; never world-open."
  type        = string
  validation {
    condition     = can(cidrnetmask(var.allowed_ssh_cidr)) && !can(regex("/0$", var.allowed_ssh_cidr))
    error_message = "Supply an IPv4 CIDR such as 203.0.113.5/32; /0 is forbidden."
  }
}

variable "repository_url" {
  description = "Public or private GitHub HTTPS clone URL without embedded credentials. Set your fork's URL to deploy a fork."
  type        = string
  default     = "https://github.com/freedomridge/freedomridge"
  validation {
    condition     = can(regex("^https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.repository_url))
    error_message = "Use a GitHub HTTPS repository URL without embedded credentials."
  }
}

variable "repository_token_file" {
  description = "Optional absolute path ON THE INSTANCE to a root-only file containing a read-only GitHub token. Never put the token in tfvars/state; see bootstrap recovery in the AWS guide."
  type        = string
  default     = ""
  validation {
    condition     = var.repository_token_file == "" || startswith(var.repository_token_file, "/")
    error_message = "Use an absolute instance path or leave empty for public cloning."
  }
}

variable "availability_zone" {
  description = "Optional AZ override; must offer instance_type. Pin the selected AZ after first deploy, especially before temporary resizes."
  type        = string
  default     = null
}

variable "repository_ref" {
  description = "Reviewed commit SHA, tag or branch to deploy."
  type        = string
  default     = "main"
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._/-]*$", var.repository_ref))
    error_message = "Use a commit SHA, tag or branch name."
  }
}

variable "public_origin" {
  description = "Required. Exact HTTPS browser origin of your domain, with no port/path/trailing slash; certificates are requested for it."
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", var.public_origin))
    error_message = "Use an HTTPS DNS origin such as https://demo.example.com."
  }
}

variable "budget_email" {
  description = "Required email for budget notifications AND CloudWatch SNS alarms (confirm the SNS email)."
  type        = string
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.budget_email))
    error_message = "Supply a notification email address."
  }
}

variable "monthly_budget_usd" {
  description = "Account-wide monthly cost alert threshold in USD; alerts do not cap spending."
  type        = number
  default     = 100
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "Budget must be positive."
  }
}

variable "data_volume_gib" {
  description = "PostgreSQL, local dumps and import workspace, plus all media unless it is in S3; see the AWS guide, section 13."
  type        = number
  default     = 300
  validation {
    condition     = var.data_volume_gib >= 50
    error_message = "Allow at least 50 GiB (database and dumps, media in S3); the full demo with media on this disk needs 150 GiB."
  }
}

variable "cache_volume_gib" {
  description = "Pruned media cache plus persistent Hugging Face weights."
  type        = number
  default     = 30
  validation {
    condition     = var.cache_volume_gib >= 20
    error_message = "Allow at least 20 GiB for cache and model weights."
  }
}
